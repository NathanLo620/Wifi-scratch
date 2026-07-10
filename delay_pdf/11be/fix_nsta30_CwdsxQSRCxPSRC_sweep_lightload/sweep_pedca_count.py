#!/usr/bin/env python3
"""
P-EDCA Count Sweep — Fixed 20 STAs, Varying P-EDCA STA Count
=============================================================
Sweeps the number of P-EDCA enabled STAs from 0 to 20 (with total 20 STAs),
running pedca_verification_nsta.cc with --pedcaRatio=nPedca/20 for each.

For each nPedca value:
  - Runs N_RUNS simulations (different RngRun seeds)
  - Averages histogram probabilities across runs
  - Collects averaged statistics

Output:
  - Delay PDF/CDF plots per nPedca
  - Packet Loss vs nPedca
  - Channel Idle vs nPedca
  - P-EDCA Tx Ratio vs nPedca (pedcaTx / edcaTx per P-EDCA STA)
  - P-EDCA Success Share vs nPedca (total pedcaTx / total successes)
  - Per-P-EDCA-STA pedcaTx / (pedcaTx + non-pedcaTx) ratio

Usage:
  python3 sweep_pedca_count.py                  # Full sweep + plot
  python3 sweep_pedca_count.py --plot-only      # Re-plot from existing data
  python3 sweep_pedca_count.py --workers 8      # Parallel workers
  python3 sweep_pedca_count.py --runs 3         # Override runs per scenario
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
from multiprocessing import cpu_count as mp_cpu_count

# ══════════════════════════════════════════════════════════════════════
#  USER-CONFIGURABLE PARAMETERS
# ══════════════════════════════════════════════════════════════════════
N_STA           = 30                                   # Fixed total STAs
PEDCA_COUNTS    = [5, 15, 30]                           # P-EDCA STA counts to run each CWds×QSRC×PSRC combo at
CWDS_VALUES     = [0, 1]                                # CWds sweep: 0..1            (2 values, runtime --cwds, no rebuild)
QSRC_VALUES     = list(range(0, 6))                    # QSRC threshold sweep: 0..5  (6 values)
PSRC_VALUES     = [1, 2, 3]                             # PSRC limit sweep: 1..3      (3 values)
BASELINE_NPEDCA = 0                                     # EDCA-only baseline = 0 P-EDCA STAs (CWds/QSRC/PSRC irrelevant)
DATA_RATE       = "1Mbps"
SIM_TIME        = 10.0
BIN_WIDTH       = 5                                    # VO delay PDF bin width (µs)
MAX_WORKERS     = 4 if not os.cpu_count() else max(1, int(os.cpu_count() // 2))
N_RUNS          = 10
SIM_BINARY      = "scratch/pedca_verification_nsta_11be.cc"
# ══════════════════════════════════════════════════════════════════════

# Paths
NS3_DIR = Path("/home/wmnlab/Desktop/ns-3.45")
OUT_DIR = Path(__file__).resolve().parent
# Header that holds the QSRC threshold (PEDCA_RETRY_THRESHOLD).
# Python patches this single line, runs `./ns3 build`, sweeps, and restores
# the original value in the finally block. The wifi model is not edited
# manually — all changes happen here.
HEADER_PATH  = NS3_DIR / "src/wifi/model/qos-frame-exchange-manager.h"
QSRC_LINE_RE = re.compile(
    r"(static\s+constexpr\s+uint16_t\s+PEDCA_RETRY_THRESHOLD\s*=\s*)(\d+)(\s*;)"
)
PSRC_LINE_RE = re.compile(
    r"(static\s+constexpr\s+uint8_t\s+PEDCA_CONSECUTIVE_ATTEMPT\s*=\s*)(\d+)(\s*;)"
)

# ── Force non-interactive backend ──
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.cm as cm


# ─────────────────────── Filename Helpers ────────────────────────────

# Module-level "current CWds / QSRC / PSRC". Set by the main sweep loop and
# read by the filename helpers (and the sim runner, for --cwds) so the existing
# call sites do not need new args. The combo tag is "c{CWds}_q{QSRC}_s{PSRC}",
# which makes every name/dir helper (routing through qsrc_tag/qsrc_dir) emit
# per-combo files in a per-combo subdirectory automatically.
CURRENT_CWDS: int = 0
CURRENT_QSRC: int = 0
CURRENT_PSRC: int = 1
BASELINE_MODE: bool = False     # while True, tag/dir resolve to the EDCA-only subdir
BASELINE_TAG  = "edca_only"

def set_current_cwds(cwds: int):
    global CURRENT_CWDS
    CURRENT_CWDS = cwds

def set_current_qsrc(qsrc: int):
    global CURRENT_QSRC
    CURRENT_QSRC = qsrc

def set_current_psrc(psrc: int):
    global CURRENT_PSRC
    CURRENT_PSRC = psrc

def set_baseline_mode(on: bool):
    global BASELINE_MODE
    BASELINE_MODE = on

def qsrc_tag(qsrc: int = None) -> str:
    """Combined CWds×QSRC×PSRC tag, e.g. 'c0_q02_s01'. In baseline mode returns
    'edca_only'. The optional qsrc arg overrides only the QSRC component."""
    if BASELINE_MODE:
        return BASELINE_TAG
    q = CURRENT_QSRC if qsrc is None else qsrc
    return f"c{CURRENT_CWDS:d}_q{q:02d}_s{CURRENT_PSRC:02d}"

def combo_tag(cwds: int, qsrc: int, psrc: int) -> str:
    return f"c{cwds:d}_q{qsrc:02d}_s{psrc:02d}"

def qsrc_dir(qsrc: int = None) -> Path:
    """Per-combo subdirectory under OUT_DIR (created on demand)."""
    d = OUT_DIR / qsrc_tag(qsrc)
    d.mkdir(parents=True, exist_ok=True)
    return d

def count_tag(n_pedca: int) -> str:
    """Return a filesystem-safe count tag, e.g. 0 -> 'p00', 5 -> 'p05'."""
    return f"p{n_pedca:02d}"

def csv_name(n_pedca: int, data_rate: str, run_idx: int = None) -> str:
    tag = f"{qsrc_tag()}_{count_tag(n_pedca)}"
    if run_idx is not None:
        return f"{tag}_vo_delay_pdf_nSta{N_STA}_{data_rate}_run{run_idx}.csv"
    return f"{tag}_vo_delay_pdf_nSta{N_STA}_{data_rate}.csv"

def pedca_sta_csv_name(n_pedca: int, data_rate: str, run_idx: int = None) -> str:
    tag = f"{qsrc_tag()}_{count_tag(n_pedca)}"
    if run_idx is not None:
        return f"{tag}_pedca_sta_delay_pdf_nSta{N_STA}_{data_rate}_run{run_idx}.csv"
    return f"{tag}_pedca_sta_delay_pdf_nSta{N_STA}_{data_rate}.csv"

def legacy_sta_csv_name(n_pedca: int, data_rate: str, run_idx: int = None) -> str:
    tag = f"{qsrc_tag()}_{count_tag(n_pedca)}"
    if run_idx is not None:
        return f"{tag}_legacy_sta_delay_pdf_nSta{N_STA}_{data_rate}_run{run_idx}.csv"
    return f"{tag}_legacy_sta_delay_pdf_nSta{N_STA}_{data_rate}.csv"

def combined_plot_name(n_pedca: int, data_rate: str) -> str:
    return f"{qsrc_tag()}_vo_delay_probability_pedca{n_pedca}_{data_rate}.pdf"

def log_name(data_rate: str) -> str:
    return f"sim_log_fixNsta{N_STA}_{qsrc_tag()}_{data_rate}.txt"


# ─────────────────────── QSRC Header Patch / Rebuild ────────────────

def read_current_qsrc() -> int:
    """Read the current PEDCA_RETRY_THRESHOLD value from the header file."""
    text = HEADER_PATH.read_text()
    m = QSRC_LINE_RE.search(text)
    if not m:
        raise RuntimeError(
            f"Could not find PEDCA_RETRY_THRESHOLD line in {HEADER_PATH}"
        )
    return int(m.group(2))

def patch_qsrc(value: int):
    """Patch PEDCA_RETRY_THRESHOLD in the header to `value`.
    Raises if the substitution does not change exactly one occurrence."""
    text = HEADER_PATH.read_text()
    new_text, n_sub = QSRC_LINE_RE.subn(
        lambda m: f"{m.group(1)}{value}{m.group(3)}", text
    )
    if n_sub != 1:
        raise RuntimeError(
            f"Expected exactly 1 PEDCA_RETRY_THRESHOLD substitution in "
            f"{HEADER_PATH}, got {n_sub}"
        )
    if new_text != text:
        HEADER_PATH.write_text(new_text)

def read_current_psrc() -> int:
    """Read the current PEDCA_CONSECUTIVE_ATTEMPT value from the header file."""
    text = HEADER_PATH.read_text()
    m = PSRC_LINE_RE.search(text)
    if not m:
        raise RuntimeError(
            f"Could not find PEDCA_CONSECUTIVE_ATTEMPT line in {HEADER_PATH}"
        )
    return int(m.group(2))

def patch_psrc(value: int):
    """Patch PEDCA_CONSECUTIVE_ATTEMPT in the header to `value`.
    Raises if the substitution does not change exactly one occurrence."""
    text = HEADER_PATH.read_text()
    new_text, n_sub = PSRC_LINE_RE.subn(
        lambda m: f"{m.group(1)}{value}{m.group(3)}", text
    )
    if n_sub != 1:
        raise RuntimeError(
            f"Expected exactly 1 PEDCA_CONSECUTIVE_ATTEMPT substitution in "
            f"{HEADER_PATH}, got {n_sub}"
        )
    if new_text != text:
        HEADER_PATH.write_text(new_text)

def rebuild_ns3():
    """Run `./ns3 build` from NS3_DIR. Aborts on failure."""
    print(f"    [build] ./ns3 build  (cwd={NS3_DIR})")
    t0 = time.time()
    proc = subprocess.run(
        ["./ns3", "build"], cwd=str(NS3_DIR),
        capture_output=True, text=True
    )
    elapsed = time.time() - t0
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-2000:])
        sys.stderr.write(proc.stderr[-2000:])
        raise RuntimeError(f"./ns3 build failed (exit {proc.returncode})")
    print(f"    [build] ok in {elapsed:.1f}s")


# ─────────────────────── Single Simulation Task ──────────────────────

def run_single_sim(n_pedca: int, data_rate: str,
                   sim_time: float, bin_us: int, run_idx: int = 0) -> dict:
    """
    Run ONE ns-3 simulation with --pedcaRatio=n_pedca/N_STA.
    Returns a dict with results. Thread-safe.
    """
    ratio = n_pedca / N_STA
    out_dir = qsrc_dir()
    csv_file = csv_name(n_pedca, data_rate, run_idx)
    csv_path = out_dir / csv_file
    relative_csv = str(csv_path.relative_to(NS3_DIR))

    pedca_csv_file = pedca_sta_csv_name(n_pedca, data_rate, run_idx)
    pedca_csv_path = out_dir / pedca_csv_file
    relative_pedca_csv = str(pedca_csv_path.relative_to(NS3_DIR))

    legacy_csv_file = legacy_sta_csv_name(n_pedca, data_rate, run_idx)
    legacy_csv_path = out_dir / legacy_csv_file
    relative_legacy_csv = str(legacy_csv_path.relative_to(NS3_DIR))

    out_dir.mkdir(parents=True, exist_ok=True)

    # CWds / QSRC / PSRC are now ALL runtime args — no rebuild needed when sweeping.
    args = (
        f"--nSta={N_STA} "
        f"--simTime={sim_time} "
        f"--dataRate={data_rate} "
        f"--pedcaRatio={ratio} "
        f"--cwds={CURRENT_CWDS} "
        f"--qsrc={CURRENT_QSRC} "
        f"--psrc={CURRENT_PSRC} "
        f"--voicePdfBinUs={bin_us} "
        f"--voicePdfOutput={relative_csv} "
        f"--pedcaStaDelayOutput={relative_pedca_csv} "
        f"--legacyStaDelayOutput={relative_legacy_csv} "
        f"--clogFile=/dev/null "
        f"--RngRun={run_idx + 1}"
    )
    cmd = ["./ns3", "run", f"{SIM_BINARY} {args}"]

    t0 = time.time()
    result_info = {
        "n_pedca":  n_pedca,
        "ratio":    ratio,
        "run_idx":  run_idx,
        "cmd":      " ".join(cmd),
        "csv_path": None,
        "pedca_csv_path": None,
        "legacy_csv_path": None,
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
        result_info["stdout"] = extract_stats_block(result.stdout)
        result_info["stderr"] = ""

        if csv_path.exists() and csv_path.stat().st_size > 10:
            result_info["csv_path"] = csv_path
            result_info["success"] = True
        if pedca_csv_path.exists() and pedca_csv_path.stat().st_size > 10:
            result_info["pedca_csv_path"] = pedca_csv_path
        if legacy_csv_path.exists() and legacy_csv_path.stat().st_size > 10:
            result_info["legacy_csv_path"] = legacy_csv_path

    except subprocess.CalledProcessError as e:
        err_out = e.stdout or ""
        result_info["stdout"] = err_out[-5000:] if len(err_out) > 5000 else err_out
        result_info["stderr"] = ""

    result_info["elapsed"] = time.time() - t0
    return result_info


# ─────────────────────── Histogram Averaging ─────────────────────────

def average_histograms(csv_paths: list, out_path: Path, n_runs: int):
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
        if "=== General Statistics ===" in line and start_idx is None:
            start_idx = i
        if start_idx is not None and "VO Delay PDF" in line:
            end_idx = i
            break
    if start_idx is None:
        return "  (no General Statistics output found)\n"
    block = lines[start_idx:end_idx]
    while block and not block[-1].strip():
        block.pop()

    # Also capture EXTENDED_STATS block
    ext_start = None
    ext_end = None
    for i, line in enumerate(lines):
        if "EXTENDED_STATS_BEGIN" in line:
            ext_start = i
        if "EXTENDED_STATS_END" in line:
            ext_end = i + 1
            break
    if ext_start is not None and ext_end is not None:
        block.extend([""])
        block.extend(lines[ext_start:ext_end])

    return "\n".join(block) + "\n"


def parse_stats(stdout: str) -> dict:
    """Parse WifiTxStatsHelper output into a structured dict."""
    block = extract_stats_block(stdout)
    result = {
        "pedca_ratio": 0.0,
        "channel_idle_ratio": 0.0,
        "avg_pedca_tx_ratio": 0.0,
        "avg_pedca_success_rate": 0.0,
        "total_pedca_tx": 0,
        "total_edca_tx": 0,
        "total_pedca_attempt": 0,
        "total_failures": 0,
        "total_retransmissions": 0,
        "total_ds_cts_sent": 0,
        "total_stage2_entry": 0,
        "total_stage2_tx": 0,
        "total_pedca_success": 0,
        "total_edca_vo_success": 0,
        "fail_rts_no_cts": 0,
        "fail_rts_collision": 0,
        "fail_timing_expired": 0,
        "fail_deferral": 0,
        "total_vo_tx": 0,
        "per_ac": {},
        "failure_ac": {},
        "failure_reasons": {},
        # Extended stats (from pedca_verification_nsta_mod)
        "pedca_sta_succ_count": 0,
        "pedca_sta_fail_count": 0,
        "pedca_sta_zero_retx": 0,
        "legacy_sta_succ_count": 0,
        "legacy_sta_fail_count": 0,
        "legacy_sta_zero_retx": 0,
        "pedca_sta_avg_mac_delay": 0.0,
        "pedca_sta_avg_queue_delay": 0.0,
        "pedca_sta_avg_access_delay": 0.0,
        "legacy_sta_avg_mac_delay": 0.0,
        "legacy_sta_avg_queue_delay": 0.0,
        "legacy_sta_avg_access_delay": 0.0,
    }

    lines = block.splitlines()
    section = None
    current_ac = None

    for line in lines:
        s = line.strip()
        if s.startswith("P-EDCA Ratio:"):
            try: result["pedca_ratio"] = float(s.split(":")[1].strip())
            except: pass
        elif s.startswith("Channel Idle Time (AP):"):
            try: result["channel_idle_ratio"] = float(s.split(":")[1].split("%")[0].strip())
            except: pass
        elif s.startswith("P-EDCA Share (Avg Per-STA P-EDCA Tx/Total Tx):"):
            try: result["avg_pedca_tx_ratio"] = float(s.split(":")[1].split("%")[0].strip()) / 100.0
            except: pass
        elif s.startswith("Avg P-EDCA Attempt Success Rate:"):
            try: result["avg_pedca_success_rate"] = float(s.split(":")[1].split("%")[0].strip()) / 100.0
            except: pass
        elif s.startswith("Global P-EDCA Tx Success:"):
            try: result["total_pedca_tx"] = float(s.split(":")[1].strip())
            except: pass
        elif s.startswith("Global EDCA Tx Success:"):
            try: result["total_edca_tx"] = float(s.split(":")[1].strip())
            except: pass
        elif s.startswith("Global P-EDCA Attempt (DS-CTS Sent):"):
            try: result["total_pedca_attempt"] = float(s.split(":")[1].strip())
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
        elif "P-EDCA Detailed Trace" in s:
            section = "pedca_trace"
        elif section == "pedca_trace" and ":" in s:
            key, _, val = s.partition(":")
            key = key.strip()
            try:
                num = float(val.strip().split()[0])
                if key == "Stage 2 Entered": result["total_stage2_entry"] = num
                elif key == "Stage 2 TX Started": result["total_stage2_tx"] = num
                elif key == "P-EDCA Fail RTS No CTS": result["fail_rts_no_cts"] = num
                elif key == "P-EDCA Fail RTS Collision": result["fail_rts_collision"] = num
                elif key == "P-EDCA Fail Timing Expired": result["fail_timing_expired"] = num
                elif key == "P-EDCA Fail Deferral": result["fail_deferral"] = num
                elif key == "Total VO TX (P-EDCA+EDCA)": result["total_vo_tx"] = num
            except: pass
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

        # Extended stats (machine-parseable block)
        ext_map = {
            "PEDCA_STA_SUCC_COUNT": "pedca_sta_succ_count",
            "PEDCA_STA_FAIL_COUNT": "pedca_sta_fail_count",
            "PEDCA_STA_ZERO_RETX": "pedca_sta_zero_retx",
            "LEGACY_STA_SUCC_COUNT": "legacy_sta_succ_count",
            "LEGACY_STA_FAIL_COUNT": "legacy_sta_fail_count",
            "LEGACY_STA_ZERO_RETX": "legacy_sta_zero_retx",
            "PEDCA_STA_AVG_MAC_DELAY": "pedca_sta_avg_mac_delay",
            "PEDCA_STA_AVG_QUEUE_DELAY": "pedca_sta_avg_queue_delay",
            "PEDCA_STA_AVG_ACCESS_DELAY": "pedca_sta_avg_access_delay",
            "LEGACY_STA_AVG_MAC_DELAY": "legacy_sta_avg_mac_delay",
            "LEGACY_STA_AVG_QUEUE_DELAY": "legacy_sta_avg_queue_delay",
            "LEGACY_STA_AVG_ACCESS_DELAY": "legacy_sta_avg_access_delay",
        }
        for ext_key, result_key in ext_map.items():
            if s.startswith(ext_key + ":"):
                try:
                    result[result_key] = float(s.split(":")[1].strip())
                except:
                    pass
                break

    return result


def average_stats(stats_list: list) -> dict:
    n = len(stats_list)
    if n == 0:
        return None

    avg = {
        "pedca_ratio": stats_list[0].get("pedca_ratio", 0.0),
        "channel_idle_ratio": sum(s.get("channel_idle_ratio", 0.0) for s in stats_list) / n,
        "avg_pedca_tx_ratio": sum(s.get("avg_pedca_tx_ratio", 0.0) for s in stats_list) / n,
        "avg_pedca_success_rate": sum(s.get("avg_pedca_success_rate", 0.0) for s in stats_list) / n,
        "total_pedca_tx": sum(s.get("total_pedca_tx", 0) for s in stats_list) / n,
        "total_edca_tx": sum(s.get("total_edca_tx", 0) for s in stats_list) / n,
        "total_pedca_attempt": sum(s.get("total_pedca_attempt", 0) for s in stats_list) / n,
        "total_successes": sum(s.get("total_successes", 0) for s in stats_list) / n,
        "total_failures": sum(s.get("total_failures", 0) for s in stats_list) / n,
        "total_retransmissions": sum(s.get("total_retransmissions", 0) for s in stats_list) / n,
        "total_ds_cts_sent": sum(s.get("total_ds_cts_sent", 0) for s in stats_list) / n,
        "total_stage2_entry": sum(s.get("total_stage2_entry", 0) for s in stats_list) / n,
        "total_stage2_tx": sum(s.get("total_stage2_tx", 0) for s in stats_list) / n,
        "total_pedca_success": sum(s.get("total_pedca_success", 0) for s in stats_list) / n,
        "total_edca_vo_success": sum(s.get("total_edca_vo_success", 0) for s in stats_list) / n,
        "fail_rts_no_cts": sum(s.get("fail_rts_no_cts", 0) for s in stats_list) / n,
        "fail_rts_collision": sum(s.get("fail_rts_collision", 0) for s in stats_list) / n,
        "fail_timing_expired": sum(s.get("fail_timing_expired", 0) for s in stats_list) / n,
        "fail_deferral": sum(s.get("fail_deferral", 0) for s in stats_list) / n,
        "total_vo_tx": sum(s.get("total_vo_tx", 0) for s in stats_list) / n,
        "per_ac": {},
        "failure_ac": {},
        "failure_reasons": {},
        # Extended stats
        "pedca_sta_succ_count": sum(s.get("pedca_sta_succ_count", 0) for s in stats_list) / n,
        "pedca_sta_fail_count": sum(s.get("pedca_sta_fail_count", 0) for s in stats_list) / n,
        "pedca_sta_zero_retx": sum(s.get("pedca_sta_zero_retx", 0) for s in stats_list) / n,
        "legacy_sta_succ_count": sum(s.get("legacy_sta_succ_count", 0) for s in stats_list) / n,
        "legacy_sta_fail_count": sum(s.get("legacy_sta_fail_count", 0) for s in stats_list) / n,
        "legacy_sta_zero_retx": sum(s.get("legacy_sta_zero_retx", 0) for s in stats_list) / n,
        "pedca_sta_avg_mac_delay": sum(s.get("pedca_sta_avg_mac_delay", 0) for s in stats_list) / n,
        "pedca_sta_avg_queue_delay": sum(s.get("pedca_sta_avg_queue_delay", 0) for s in stats_list) / n,
        "pedca_sta_avg_access_delay": sum(s.get("pedca_sta_avg_access_delay", 0) for s in stats_list) / n,
        "legacy_sta_avg_mac_delay": sum(s.get("legacy_sta_avg_mac_delay", 0) for s in stats_list) / n,
        "legacy_sta_avg_queue_delay": sum(s.get("legacy_sta_avg_queue_delay", 0) for s in stats_list) / n,
        "legacy_sta_avg_access_delay": sum(s.get("legacy_sta_avg_access_delay", 0) for s in stats_list) / n,
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
    lines = []
    lines.append(f"=== WifiTxStatsHelper (MAC-layer) [Averaged over {n_runs} runs] ===")
    lines.append(f"P-EDCA Ratio: {avg['pedca_ratio']}")
    lines.append(f"Channel Idle Time (AP): {avg.get('channel_idle_ratio', 0.0):.2f} %")
    lines.append(f"P-EDCA Share (Avg Per-STA P-EDCA Tx/Total Tx): {avg.get('avg_pedca_tx_ratio', 0.0) * 100.0:.6g} %")
    lines.append(f"Avg P-EDCA Attempt Success Rate: {avg.get('avg_pedca_success_rate', 0.0) * 100.0:.6g} %")
    lines.append(f"Global P-EDCA Tx Success: {avg.get('total_pedca_tx', 0):.1f}")
    lines.append(f"Global EDCA Tx Success: {avg.get('total_edca_tx', 0):.1f}")
    lines.append(f"Global P-EDCA Attempt (DS-CTS Sent): {avg.get('total_pedca_attempt', 0):.1f}")
    if avg.get("total_successes", 0) > 0:
        lines.append(f"Total Successes:       {avg.get('total_successes', 0):.1f}")
    if avg.get("total_failures", 0) > 0:
        lines.append(f"Total Failures:        {avg.get('total_failures', 0):.1f}")
    if avg.get("total_retransmissions", 0) > 0:
        lines.append(f"Total Retransmissions: {avg.get('total_retransmissions', 0):.1f}")
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

    lines.append("")
    lines.append("--- P-EDCA Detailed Trace ---")
    lines.append(f"DS-CTS Sent: {avg.get('total_pedca_attempt', 0):.1f}")
    lines.append(f"Stage 2 Entered: {avg.get('total_stage2_entry', 0):.1f}")
    lines.append(f"Stage 2 TX Started: {avg.get('total_stage2_tx', 0):.1f}")
    lines.append(f"P-EDCA TX Success: {avg.get('total_pedca_tx', 0):.1f}")
    lines.append(f"EDCA VO TX Success: {avg.get('total_edca_tx', 0):.1f}")
    lines.append(f"P-EDCA Fail RTS No CTS: {avg.get('fail_rts_no_cts', 0):.1f}")
    lines.append(f"P-EDCA Fail RTS Collision: {avg.get('fail_rts_collision', 0):.1f}")
    lines.append(f"P-EDCA Fail Timing Expired: {avg.get('fail_timing_expired', 0):.1f}")
    lines.append(f"P-EDCA Fail Deferral: {avg.get('fail_deferral', 0):.1f}")
    
    total_vo = avg.get('total_vo_tx', 0)
    lines.append(f"Total VO TX (P-EDCA+EDCA): {total_vo:.1f}")
    
    pedca_ratio = (avg.get('total_pedca_tx', 0) / total_vo * 100.0) if total_vo > 0 else 0.0
    edca_ratio = (avg.get('total_edca_tx', 0) / total_vo * 100.0) if total_vo > 0 else 0.0
    
    lines.append(f"P-EDCA Success Ratio: {pedca_ratio:.4f} %")
    lines.append(f"EDCA Success Ratio: {edca_ratio:.4f} %")

    # Extended stats
    lines.append("")
    lines.append("--- Extended P-EDCA vs Legacy Statistics ---")
    lines.append(f"PEDCA_STA_SUCC_COUNT: {avg.get('pedca_sta_succ_count', 0):.1f}")
    lines.append(f"PEDCA_STA_FAIL_COUNT: {avg.get('pedca_sta_fail_count', 0):.1f}")
    lines.append(f"PEDCA_STA_ZERO_RETX: {avg.get('pedca_sta_zero_retx', 0):.1f}")
    lines.append(f"LEGACY_STA_SUCC_COUNT: {avg.get('legacy_sta_succ_count', 0):.1f}")
    lines.append(f"LEGACY_STA_FAIL_COUNT: {avg.get('legacy_sta_fail_count', 0):.1f}")
    lines.append(f"LEGACY_STA_ZERO_RETX: {avg.get('legacy_sta_zero_retx', 0):.1f}")
    lines.append(f"PEDCA_STA_AVG_MAC_DELAY: {avg.get('pedca_sta_avg_mac_delay', 0):.3f}")
    lines.append(f"PEDCA_STA_AVG_QUEUE_DELAY: {avg.get('pedca_sta_avg_queue_delay', 0):.3f}")
    lines.append(f"PEDCA_STA_AVG_ACCESS_DELAY: {avg.get('pedca_sta_avg_access_delay', 0):.3f}")
    lines.append(f"LEGACY_STA_AVG_MAC_DELAY: {avg.get('legacy_sta_avg_mac_delay', 0):.3f}")
    lines.append(f"LEGACY_STA_AVG_QUEUE_DELAY: {avg.get('legacy_sta_avg_queue_delay', 0):.3f}")
    lines.append(f"LEGACY_STA_AVG_ACCESS_DELAY: {avg.get('legacy_sta_avg_access_delay', 0):.3f}")

    return "\n".join(lines) + "\n"


# ─────────────────────── Multi-Run Group Execution ───────────────────

def aggregate_runs(run_results: list, n_pedca: int,
                   data_rate: str, n_runs: int) -> dict:
    csv_paths = [r["csv_path"] for r in run_results if r["csv_path"]]
    pedca_csv_paths = [r["pedca_csv_path"] for r in run_results if r.get("pedca_csv_path")]
    legacy_csv_paths = [r["legacy_csv_path"] for r in run_results if r.get("legacy_csv_path")]

    out_dir = qsrc_dir()
    avg_csv = out_dir / csv_name(n_pedca, data_rate)
    avg_pedca_csv = out_dir / pedca_sta_csv_name(n_pedca, data_rate)
    avg_legacy_csv = out_dir / legacy_sta_csv_name(n_pedca, data_rate)

    for paths, out in [(csv_paths, avg_csv), (pedca_csv_paths, avg_pedca_csv),
                       (legacy_csv_paths, avg_legacy_csv)]:
        if paths:
            average_histograms(paths, out, n_runs)
            for cp in paths:
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
        "n_pedca":   n_pedca,
        "ratio":     n_pedca / N_STA,
        "csv_path":  avg_csv if csv_paths else None,
        "pedca_csv_path": avg_pedca_csv if pedca_csv_paths else None,
        "legacy_csv_path": avg_legacy_csv if legacy_csv_paths else None,
        "stdout":    avg_stdout,
        "elapsed":   total_elapsed,
        "n_success": n_success,
        "n_runs":    n_runs,
    }


# ─────────────────────── Log Writer ──────────────────────────────────

def write_log(data_rate: str, results: dict, n_runs: int, pedca_counts: list):
    log_path = qsrc_dir() / log_name(data_rate)
    with open(log_path, "w") as f:
        f.write(f"{'='*70}\n")
        f.write(f"  Simulation Log — nSta={N_STA} (fixed)  dataRate={data_rate}\n")
        f.write(f"  P-EDCA STA counts: {pedca_counts}\n")
        f.write(f"  Runs per config: {n_runs}\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*70}\n\n")

        for n_pedca in pedca_counts:
            if n_pedca not in results:
                continue
            r = results[n_pedca]
            f.write(f"{'─'*70}\n")
            f.write(f"  P-EDCA STAs = {n_pedca}/{N_STA}  (ratio={n_pedca/N_STA:.2f})\n")
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

    prob_lookup = {}
    for r in rows:
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


def compute_percentiles_from_histogram(mids: list, probs: list,
                                        percentiles: list) -> dict:
    """Compute delay percentiles from a probability histogram.
    percentiles: list of floats in [0,1], e.g. [0.5, 0.95, 0.99, 0.999]
    Returns {p: delay_us} dict."""
    total = sum(probs)
    if total <= 0:
        return {p: 0.0 for p in percentiles}
    result = {}
    targets = sorted(percentiles)
    target_idx = 0
    running = 0.0
    for m, p in zip(mids, probs):
        running += p
        while target_idx < len(targets) and running / total >= targets[target_idx]:
            result[targets[target_idx]] = m
            target_idx += 1
        if target_idx >= len(targets):
            break
    for pct in targets:
        if pct not in result:
            result[pct] = mids[-1] if mids else 0.0
    return result


_PCT_LEVELS = [0.5, 0.95, 0.99, 0.999]
_PCT_TAGS   = {0.5: "P50", 0.95: "P95", 0.99: "P99", 0.999: "P99.9"}

def _add_cdf_percentile_markers(ax, mids: list, probs: list,
                                  zoom_xmax: float, color,
                                  linestyle: str = "--"):
    """Draw vertical dashed lines for percentiles that fall within zoom_xmax.
    Rule: only mark a percentile if its x-value <= zoom_xmax."""
    pcts = compute_percentiles_from_histogram(mids, probs, _PCT_LEVELS)
    for p in _PCT_LEVELS:
        x_val = pcts[p]
        if x_val <= zoom_xmax:
            ax.axvline(x=x_val, color=color, linewidth=0.7,
                       linestyle=linestyle, alpha=0.55)


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

def get_count_colors(counts: list) -> dict:
    n = len(counts)
    cmap = cm.get_cmap("viridis", n)
    return {c: cmap(i) for i, c in enumerate(counts)}


# ─────────────────── Statistics Comparison File ──────────────────────

def write_comparison_stats(results: dict, data_rate: str,
                           n_runs: int, pedca_counts: list):
    stats_path = qsrc_dir() / f"pedca_count_sweep_statistics_{qsrc_tag()}_{data_rate}.txt"
    with open(stats_path, "w") as f:
        f.write(f"{'='*100}\n")
        f.write(f"  P-EDCA Count Sweep Statistics (Fixed nSta={N_STA})\n")
        f.write(f"  dataRate = {data_rate}    simTime = {SIM_TIME}s    "
                f"runs = {n_runs} (averaged)\n")
        f.write(f"  P-EDCA counts: {pedca_counts}\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*100}\n\n")

        for n_pedca in pedca_counts:
            if n_pedca in results and results[n_pedca]["success"]:
                r = results[n_pedca]
                f.write(f"P-EDCA STAs = {n_pedca}/{N_STA}  (ratio={n_pedca/N_STA:.2f})\n")
                f.write(r["stdout"])
                f.write(f"\n")
            else:
                f.write(f"P-EDCA STAs = {n_pedca}/{N_STA}\n")
                f.write(f"  (simulation failed or not run)\n\n")

    return stats_path


# ─────────── Parse stats file for plots ───────────

def parse_stats_file_for_metric(stats_path: Path, pedca_counts: list,
                                metric_name: str) -> dict:
    """
    Generic parser: extract a named metric from the stats file.
    metric_name can be: 'Channel Idle Time', 'Avg P-EDCA Tx Ratio',
    'Packet Loss', 'Successes', 'Throughput', etc.
    Returns: {n_pedca: value}
    """
    result = {}
    if not stats_path.exists():
        return result

    text = stats_path.read_text()
    current_n_pedca = None
    in_ac_vo = False

    for line in text.splitlines():
        s = line.strip()

        # Match "P-EDCA STAs = 5/20  (ratio=0.25)"
        m = re.match(r"P-EDCA STAs\s*=\s*(\d+)/\d+", s)
        if m:
            current_n_pedca = int(m.group(1))
            in_ac_vo = False
            continue

        if current_n_pedca is None:
            continue

        # Channel Idle
        if metric_name == "channel_idle" and s.startswith("Channel Idle Time (AP):"):
            m2 = re.search(r"([\d.]+)\s*%", s)
            if m2:
                result[current_n_pedca] = float(m2.group(1))

        # Avg P-EDCA Tx Ratio
        # Avg P-EDCA Tx Ratio
        elif metric_name == "pedca_tx_ratio" and s.startswith("P-EDCA Share (Avg Per-STA P-EDCA Tx/Total Tx):"):
            try:
                val = float(s.split(":")[1].split("%")[0].strip()) / 100.0
                result[current_n_pedca] = val
            except:
                pass

        # Avg P-EDCA Success Rate (pedcaTx / pedcaAttempt)
        elif metric_name == "pedca_success_rate" and s.startswith("Avg P-EDCA Attempt Success Rate:"):
            try:
                val = float(s.split(":")[1].split("%")[0].strip()) / 100.0
                result[current_n_pedca] = val
            except:
                pass

        # Total P-EDCA Tx
        elif metric_name == "total_pedca_tx" and s.startswith("Global P-EDCA Tx Success:"):
            try:
                result[current_n_pedca] = float(s.split(":")[1].strip())
            except:
                pass

        # Total EDCA Tx
        elif metric_name == "total_edca_tx" and s.startswith("Global EDCA Tx Success:"):
            try:
                result[current_n_pedca] = float(s.split(":")[1].strip())
            except:
                pass

        # Total P-EDCA Attempt
        elif metric_name == "total_pedca_attempt" and s.startswith("Global P-EDCA Attempt (DS-CTS Sent):"):
            try:
                result[current_n_pedca] = float(s.split(":")[1].strip())
            except:
                pass
                
        elif metric_name == "total_stage2_tx" and s.startswith("Stage 2 TX Started:"):
            try: result[current_n_pedca] = float(s.split(":")[1].strip())
            except: pass
        elif metric_name == "total_stage2_entry" and s.startswith("Stage 2 Entered:"):
            try: result[current_n_pedca] = float(s.split(":")[1].strip())
            except: pass
        elif metric_name == "fail_rts_no_cts" and s.startswith("P-EDCA Fail RTS No CTS:"):
            try: result[current_n_pedca] = float(s.split(":")[1].strip())
            except: pass
        elif metric_name == "fail_rts_collision" and s.startswith("P-EDCA Fail RTS Collision:"):
            try: result[current_n_pedca] = float(s.split(":")[1].strip())
            except: pass
        elif metric_name == "fail_timing_expired" and s.startswith("P-EDCA Fail Timing Expired:"):
            try: result[current_n_pedca] = float(s.split(":")[1].strip())
            except: pass
        elif metric_name == "fail_deferral" and s.startswith("P-EDCA Fail Deferral:"):
            try: result[current_n_pedca] = float(s.split(":")[1].strip())
            except: pass

        # Total Successes
        elif metric_name == "total_successes" and (s.startswith("Total Successes:") or s.startswith("Total VO TX (P-EDCA+EDCA):")):
            try:
                result[current_n_pedca] = float(s.split(":")[1].strip())
            except:
                pass

        # Total Failures
        elif metric_name == "total_failures" and s.startswith("Total Failures:"):
            try:
                result[current_n_pedca] = float(s.split(":")[1].strip())
            except:
                pass

        # Detect AC_VO section
        elif s == "AC_VO:":
            in_ac_vo = True
            continue
        elif s.startswith("AC_") and s.endswith(":") and s != "AC_VO:":
            in_ac_vo = False
            continue
        elif s.startswith("---"):
            in_ac_vo = False
            continue

        # AC_VO specific metrics
        elif in_ac_vo:
            if metric_name == "vo_packet_loss" and s.startswith("Packet Loss:"):
                m2 = re.search(r"([\d.]+)\s*%", s)
                if m2:
                    result[current_n_pedca] = float(m2.group(1))
                in_ac_vo = False

            elif metric_name == "vo_throughput" and s.startswith("Throughput:"):
                m2 = re.search(r"([\d.]+)\s*Mbps", s)
                if m2:
                    result[current_n_pedca] = float(m2.group(1))

            elif metric_name == "vo_mac_delay" and s.startswith("Avg MAC Delay:"):
                m2 = re.search(r"([\d.]+)\s*us", s)
                if m2:
                    result[current_n_pedca] = float(m2.group(1))

            elif metric_name == "vo_successes" and s.startswith("Successes:"):
                try:
                    result[current_n_pedca] = float(s.split(":")[1].strip())
                except:
                    pass

        # Extended stats (machine-parseable keys)
        ext_metrics = {
            "pedca_sta_succ_count": "PEDCA_STA_SUCC_COUNT",
            "pedca_sta_fail_count": "PEDCA_STA_FAIL_COUNT",
            "pedca_sta_zero_retx": "PEDCA_STA_ZERO_RETX",
            "legacy_sta_succ_count": "LEGACY_STA_SUCC_COUNT",
            "legacy_sta_fail_count": "LEGACY_STA_FAIL_COUNT",
            "legacy_sta_zero_retx": "LEGACY_STA_ZERO_RETX",
            "pedca_sta_avg_mac_delay": "PEDCA_STA_AVG_MAC_DELAY",
            "legacy_sta_avg_mac_delay": "LEGACY_STA_AVG_MAC_DELAY",
        }
        if metric_name in ext_metrics:
            prefix = ext_metrics[metric_name] + ":"
            if s.startswith(prefix):
                try:
                    result[current_n_pedca] = float(s.split(":")[1].strip())
                except:
                    pass

    return result


# ─────────── Plot Helpers ───────────

def plot_metric_vs_pedca_count(stats_path: Path, out_path: Path,
                                metric_name: str, ylabel: str, title: str,
                                data_rate: str, pedca_counts: list,
                                n_runs: int = 1,
                                fig_width: float = 12.0,
                                fig_height: float = 6.0,
                                dpi: int = 200):
    """Generic plotter: one metric vs nPedca."""
    data = parse_stats_file_for_metric(stats_path, pedca_counts, metric_name)

    if not data:
        return None

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    x_vals = sorted(data.keys())
    y_vals = [data[x] for x in x_vals]

    runs_label = f" (avg of {n_runs} runs)" if n_runs > 1 else ""

    ax.plot(x_vals, y_vals, marker="o", markersize=5, linewidth=1.5,
            color="#4C72B0", label=f"nSta={N_STA}")
    ax.fill_between(x_vals, y_vals, alpha=0.1, color="#4C72B0")

    ax.set_xlabel("Number of P-EDCA STAs", fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.set_title(f"{title}  —  nSta={N_STA}, {data_rate}{runs_label}",
                 fontsize=13, fontweight="bold")
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.set_xticks(x_vals)
    ax.tick_params(axis="x", labelsize=8, rotation=45)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


def compute_pedca_success_share(stats_path: Path, pedca_counts: list) -> dict:
    """
    Compute exact P-EDCA success share:
      = Total P-EDCA Tx / Total EDCA Tx
    Using exact counts from the simulation output.
    """
    pedca_tx_data = parse_stats_file_for_metric(stats_path, pedca_counts, "total_pedca_tx")
    edca_tx_data = parse_stats_file_for_metric(stats_path, pedca_counts, "total_edca_tx")
    
    result = {}
    for n_pedca in pedca_counts:
        if n_pedca == 0:
            result[n_pedca] = 0.0
        elif n_pedca in pedca_tx_data and n_pedca in edca_tx_data:
            total_pedca = pedca_tx_data[n_pedca]
            total_edca = edca_tx_data[n_pedca]
            if total_edca > 0:
                result[n_pedca] = total_pedca / total_edca
            else:
                result[n_pedca] = 0.0
    
    return result


def plot_pedca_success_share(stats_path: Path, out_path: Path,
                              data_rate: str, pedca_counts: list,
                              n_runs: int = 1,
                              fig_width: float = 12.0,
                              fig_height: float = 6.0,
                              dpi: int = 200):
    """
    Plot P-EDCA success share vs nPedca:
    = Total P-EDCA Tx / Total EDCA Tx (exact)
    """
    data = compute_pedca_success_share(stats_path, pedca_counts)
    if not data:
        return None

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    x_vals = sorted(data.keys())
    y_vals = [data[x] for x in x_vals]

    runs_label = f" (avg of {n_runs} runs)" if n_runs > 1 else ""

    ax.plot(x_vals, y_vals, marker="s", markersize=5, linewidth=1.5,
            color="#55A868", label=f"nSta={N_STA}")
    ax.fill_between(x_vals, y_vals, alpha=0.1, color="#55A868")

    ax.set_xlabel("Number of P-EDCA STAs", fontsize=11)
    ax.set_ylabel("P-EDCA Success Share\n(Total P-EDCA Tx / Total EDCA Tx)", fontsize=11)
    ax.set_title(
        f"P-EDCA Tx / Total EDCA Tx  —  nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=13, fontweight="bold"
    )
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.set_xticks(x_vals)
    ax.tick_params(axis="x", labelsize=8, rotation=45)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


def plot_pedca_failure_breakdown(stats_path: Path, out_path: Path,
                                 data_rate: str, pedca_counts: list,
                                 n_runs: int = 1,
                                 fig_width: float = 12.0,
                                 fig_height: float = 6.0,
                                 dpi: int = 200):
    """
    Plot stacked bar chart of P-EDCA failure reasons.
    """
    fail_rts_no_cts = parse_stats_file_for_metric(stats_path, pedca_counts, "fail_rts_no_cts")
    fail_rts_coll = parse_stats_file_for_metric(stats_path, pedca_counts, "fail_rts_collision")
    fail_expired = parse_stats_file_for_metric(stats_path, pedca_counts, "fail_timing_expired")
    fail_defer = parse_stats_file_for_metric(stats_path, pedca_counts, "fail_deferral")
    
    if not fail_expired:
        return None

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    x_vals = sorted(fail_expired.keys())
    
    y_cts = np.array([fail_rts_no_cts.get(x, 0) for x in x_vals])
    y_coll = np.array([fail_rts_coll.get(x, 0) for x in x_vals])
    y_exp = np.array([fail_expired.get(x, 0) for x in x_vals])
    y_def = np.array([fail_defer.get(x, 0) for x in x_vals])
    
    runs_label = f" (avg of {n_runs} runs)" if n_runs > 1 else ""

    bar_width = 0.6
    ax.bar(x_vals, y_exp, width=bar_width, label="Timing Expired (>77us, Legacy Steal)", color="#d62728")
    bottom = y_exp
    ax.bar(x_vals, y_coll, width=bar_width, bottom=bottom, label="RTS Collision (Stage 2)", color="#ff7f0e")
    bottom += y_coll
    ax.bar(x_vals, y_cts, width=bar_width, bottom=bottom, label="CTS Timeout (No AP Reply)", color="#1f77b4")
    bottom += y_cts
    ax.bar(x_vals, y_def, width=bar_width, bottom=bottom, label="Deferral (Medium Busy)", color="#9467bd")

    ax.set_xlabel("Number of P-EDCA STAs", fontsize=11)
    ax.set_ylabel("Average Failure Count (per simulation)", fontsize=11)
    ax.set_title(
        f"P-EDCA Failure Reasons Breakdown  —  nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=13, fontweight="bold"
    )
    ax.legend(loc="upper left")
    ax.grid(True, axis="y", alpha=0.3, linestyle="--")
    ax.set_xticks(x_vals)
    ax.tick_params(axis="x", labelsize=8, rotation=45)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path

def plot_combined_metrics(stats_path: Path, out_path: Path,
                           data_rate: str, pedca_counts: list,
                           n_runs: int = 1,
                           fig_width: float = 14.0,
                           fig_height: float = 18.0,
                           dpi: int = 200):
    """
    Generate a multi-subplot figure with all key metrics vs nPedca.
    6 subplots:
      1. VO Throughput
      2. VO Packet Loss
      3. VO Avg MAC Delay
      4. Channel Idle Time
      5. Avg P-EDCA Tx Ratio (per P-EDCA STA: pedcaTx/edcaTx)
      6. Estimated P-EDCA Success Share (global)
    """
    metrics = [
        ("vo_throughput", "VO Throughput (Mbps)", "VO Throughput vs P-EDCA STAs", "#4C72B0"),
        ("vo_packet_loss", "VO Packet Loss (%)", "VO Packet Loss vs P-EDCA STAs", "#C44E52"),
        ("vo_mac_delay", "VO Avg MAC Delay (µs)", "VO Avg MAC Delay vs P-EDCA STAs", "#DD8452"),
        ("channel_idle", "Channel Idle Time (%)", "Channel Idle Time vs P-EDCA STAs", "#55A868"),
        ("pedca_tx_ratio", "Per-STA P-EDCA Tx Ratio\n(pedcaTx/edcaTx per P-EDCA STA)", 
         "Per-P-EDCA-STA: pedcaTx / edcaTx", "#8172B3"),
        ("pedca_success_rate", "P-EDCA Success Rate\n(pedcaTx/pedcaAttempt per P-EDCA STA)",
         "P-EDCA Success Rate: pedcaTx / pedcaAttempt", "#C44E52"),
    ]

    fig, axes = plt.subplots(len(metrics) + 1, 1, figsize=(fig_width, fig_height))
    runs_label = f" (avg of {n_runs} runs)" if n_runs > 1 else ""

    for idx, (metric_name, ylabel, title, color) in enumerate(metrics):
        ax = axes[idx]
        data = parse_stats_file_for_metric(stats_path, pedca_counts, metric_name)
        if data:
            x_vals = sorted(data.keys())
            y_vals = [data[x] for x in x_vals]
            ax.plot(x_vals, y_vals, marker="o", markersize=4, linewidth=1.2, color=color)
            ax.fill_between(x_vals, y_vals, alpha=0.08, color=color)
            ax.set_xticks(x_vals)
        ax.set_xlabel("Number of P-EDCA STAs", fontsize=9)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.set_title(f"{title}  —  nSta={N_STA}, {data_rate}{runs_label}",
                     fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.3, linestyle="--")
        ax.tick_params(axis="x", labelsize=7, rotation=45)

    # Last subplot: P-EDCA Success Share
    ax = axes[-1]
    share_data = compute_pedca_success_share(stats_path, pedca_counts)
    if share_data:
        x_vals = sorted(share_data.keys())
        y_vals = [share_data[x] for x in x_vals]
        ax.plot(x_vals, y_vals, marker="s", markersize=4, linewidth=1.2, color="#C4A000")
        ax.fill_between(x_vals, y_vals, alpha=0.08, color="#C4A000")
        ax.set_xticks(x_vals)
    ax.set_xlabel("Number of P-EDCA STAs", fontsize=9)
    ax.set_ylabel("P-EDCA Success Share\n(Total P-EDCA Tx / Total EDCA Tx)", fontsize=9)
    ax.set_title(
        f"P-EDCA Tx / Total EDCA Tx  —  nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=11, fontweight="bold"
    )
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.tick_params(axis="x", labelsize=7, rotation=45)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


def _compute_percentile_xlim(all_series: list, percentile: float = 0.95) -> float:
    combined = defaultdict(float)
    for mids, probs in all_series:
        for m, p in zip(mids, probs):
            combined[m] += p
    if not combined:
        return float("inf")
    n_series = len(all_series)
    sorted_mids = sorted(combined.keys())
    total = sum(combined.values()) / n_series
    if total <= 0:
        return float("inf")
    cumulative = 0.0
    target = percentile * total
    for m in sorted_mids:
        cumulative += combined[m] / n_series
        if cumulative >= target:
            return m
    return sorted_mids[-1] if sorted_mids else float("inf")


def plot_delay_pdf_overlay(results: dict, out_path: Path,
                            data_rate: str, pedca_counts: list,
                            n_runs: int = 1,
                            fig_width: float = 14.0,
                            fig_height: float = 10.0,
                            dpi: int = 200):
    """
    Overlay PDF and CDF for selected pedca counts on one figure.
    """
    # Select a subset for readability (0, 5, 10, 15, 20)
    selected = [c for c in [0, 5, 10, 15, 20] if c in pedca_counts]
    if not selected:
        selected = pedca_counts

    colors = get_count_colors(selected)
    fig, (ax_pdf, ax_cdf) = plt.subplots(2, 1, figsize=(fig_width, fig_height))

    loaded_data = {}
    all_series = []
    global_xmin = float("inf")

    for n_pedca in selected:
        csv_path = qsrc_dir() / csv_name(n_pedca, data_rate)
        if not csv_path.exists():
            continue
        try:
            mids, probs, xmin, xmax, bw = load_histogram(csv_path)
        except Exception:
            continue
        loaded_data[n_pedca] = (mids, probs, bw)
        all_series.append((mids, probs))
        global_xmin = min(global_xmin, xmin)

    if not loaded_data:
        plt.close(fig)
        return None

    x_95 = _compute_percentile_xlim(all_series, 0.95)
    zoom_xmax = x_95 * 1.10

    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""

    # PDF
    for n_pedca in selected:
        if n_pedca not in loaded_data:
            continue
        mids, probs, bw = loaded_data[n_pedca]
        z_mids = [m for m in mids if m <= zoom_xmax]
        z_probs = [p for m, p in zip(mids, probs) if m <= zoom_xmax]
        if not z_mids:
            continue
        label = "Pure EDCA (0 P-EDCA STAs)" if n_pedca == 0 else f"P-EDCA {n_pedca}/{N_STA}"
        color = colors[n_pedca]
        ax_pdf.plot(z_mids, z_probs, linewidth=0.8, color=color, label=label)
        ax_pdf.fill_between(z_mids, z_probs, alpha=0.08, color=color)

    ax_pdf.set_xlim(global_xmin, zoom_xmax)
    ticks = build_ticks(global_xmin, zoom_xmax, fig_width)
    ax_pdf.set_xticks(ticks)
    ax_pdf.tick_params(axis="x", labelsize=8, rotation=45)
    ax_pdf.set_xlabel("Delay (µs)", fontsize=10)
    ax_pdf.set_ylabel("Probability", fontsize=11)
    ax_pdf.set_title(
        f"VO Delay PDF (zoomed to 95th pctl)  —  nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    ax_pdf.grid(True, alpha=0.25, linestyle="--")
    ax_pdf.legend(loc="upper right", fontsize=8)

    # CDF
    for n_pedca in selected:
        if n_pedca not in loaded_data:
            continue
        mids, probs, bw = loaded_data[n_pedca]
        full_total = sum(probs)
        if full_total <= 0:
            continue
        cdf_mids, cdf_vals = [], []
        running = 0.0
        for m, p in zip(mids, probs):
            running += p
            if m <= zoom_xmax:
                cdf_mids.append(m)
                cdf_vals.append(running / full_total)
        if not cdf_mids:
            continue
        label = "Pure EDCA (0 P-EDCA STAs)" if n_pedca == 0 else f"P-EDCA {n_pedca}/{N_STA}"
        color = colors[n_pedca]
        ax_cdf.plot(cdf_mids, cdf_vals, linewidth=1.0, color=color, label=label)
        _add_cdf_percentile_markers(ax_cdf, mids, probs, zoom_xmax, color)

    ax_cdf.set_xlim(global_xmin, zoom_xmax)
    ticks = build_ticks(global_xmin, zoom_xmax, fig_width)
    ax_cdf.set_xticks(ticks)
    ax_cdf.set_ylim(0, 1.02)
    ax_cdf.tick_params(axis="x", labelsize=8, rotation=45)
    ax_cdf.set_xlabel("Delay (µs)", fontsize=10)
    ax_cdf.set_ylabel("Cumulative Probability", fontsize=11)
    ax_cdf.set_title(
        f"VO Delay CDF  —  nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    ax_cdf.grid(True, alpha=0.25, linestyle="--")
    ax_cdf.legend(loc="lower right", fontsize=8)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


# ─────────── NEW: Per-STA-Type Delay PDF/CDF Overlay ─────────────────

def plot_sta_type_delay_overlay(out_path: Path, data_rate: str,
                                pedca_counts: list, sta_type: str,
                                n_runs: int = 1,
                                fig_width: float = 14.0,
                                fig_height: float = 10.0,
                                dpi: int = 200):
    """
    Overlay delay PDF and CDF for a specific STA type (P-EDCA or Legacy)
    across different nPedca values.
    sta_type: "pedca" or "legacy"
    """
    selected = [c for c in [0, 1, 5, 10, 15, 20] if c in pedca_counts]
    if not selected:
        selected = pedca_counts

    # Filter: pedca only for n_pedca>0, legacy only for n_pedca<N_STA
    if sta_type == "pedca":
        selected = [c for c in selected if c > 0]
        csv_fn = pedca_sta_csv_name
        type_label = "P-EDCA STAs"
    else:
        selected = [c for c in selected if c < N_STA]
        csv_fn = legacy_sta_csv_name
        type_label = "Legacy EDCA STAs"

    if not selected:
        return None

    colors = get_count_colors(selected)
    fig, (ax_pdf, ax_cdf) = plt.subplots(2, 1, figsize=(fig_width, fig_height))

    loaded_data = {}
    all_series = []
    global_xmin = float("inf")

    for n_pedca in selected:
        csv_path = qsrc_dir() / csv_fn(n_pedca, data_rate)
        if not csv_path.exists():
            continue
        try:
            mids, probs, xmin, xmax, bw = load_histogram(csv_path)
        except Exception:
            continue
        loaded_data[n_pedca] = (mids, probs, bw)
        all_series.append((mids, probs))
        global_xmin = min(global_xmin, xmin)

    # For pedca type: load the combined all-STA CSV at n_pedca=0 as Pure EDCA reference
    edca_ref_data = None
    if sta_type == "pedca":
        edca_ref_csv = qsrc_dir() / csv_name(0, data_rate)
        if edca_ref_csv.exists():
            try:
                ref_mids, ref_probs, ref_xmin, ref_xmax, ref_bw = load_histogram(edca_ref_csv)
                edca_ref_data = (ref_mids, ref_probs)
                all_series.append((ref_mids, ref_probs))
                global_xmin = min(global_xmin, ref_xmin)
            except Exception:
                edca_ref_data = None

    if not loaded_data and edca_ref_data is None:
        plt.close(fig)
        return None

    x_95 = _compute_percentile_xlim(all_series, 0.95)
    zoom_xmax = x_95 * 1.10
    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""

    # PDF
    for n_pedca in selected:
        if n_pedca not in loaded_data:
            continue
        mids, probs, bw = loaded_data[n_pedca]
        z_mids = [m for m in mids if m <= zoom_xmax]
        z_probs = [p for m, p in zip(mids, probs) if m <= zoom_xmax]
        if not z_mids:
            continue
        n_legacy = N_STA - n_pedca
        if sta_type == "legacy" and n_pedca == 0:
            label = "Pure EDCA (0 P-EDCA STAs)"
        else:
            label = f"P-EDCA {n_pedca} / Legacy {n_legacy}"
        color = colors[n_pedca]
        ax_pdf.plot(z_mids, z_probs, linewidth=0.8, color=color, label=label)
        ax_pdf.fill_between(z_mids, z_probs, alpha=0.06, color=color)

    ax_pdf.set_xlim(global_xmin, zoom_xmax)
    ticks = build_ticks(global_xmin, zoom_xmax, fig_width)
    ax_pdf.set_xticks(ticks)
    ax_pdf.tick_params(axis="x", labelsize=8, rotation=45)
    ax_pdf.set_xlabel("Delay (us)", fontsize=10)
    ax_pdf.set_ylabel("Probability", fontsize=11)
    ax_pdf.set_title(
        f"{type_label} Delay PDF (zoomed to 95th pctl)  —  nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    ax_pdf.grid(True, alpha=0.25, linestyle="--")
    ax_pdf.legend(loc="upper right", fontsize=8)

    # CDF
    for n_pedca in selected:
        if n_pedca not in loaded_data:
            continue
        mids, probs, bw = loaded_data[n_pedca]
        full_total = sum(probs)
        if full_total <= 0:
            continue
        cdf_mids, cdf_vals = [], []
        running = 0.0
        for m, p in zip(mids, probs):
            running += p
            if m <= zoom_xmax:
                cdf_mids.append(m)
                cdf_vals.append(running / full_total)
        if not cdf_mids:
            continue
        n_legacy = N_STA - n_pedca
        if sta_type == "legacy" and n_pedca == 0:
            label = "Pure EDCA (0 P-EDCA STAs)"
        else:
            label = f"P-EDCA {n_pedca} / Legacy {n_legacy}"
        color = colors[n_pedca]
        ax_cdf.plot(cdf_mids, cdf_vals, linewidth=1.0, color=color, label=label)
        _add_cdf_percentile_markers(ax_cdf, mids, probs, zoom_xmax, color)

    # For pedca type: draw Pure EDCA reference (combined n_pedca=0)
    if sta_type == "pedca" and edca_ref_data is not None:
        ref_mids, ref_probs = edca_ref_data
        ref_total = sum(ref_probs)
        if ref_total > 0:
            ref_cdf_mids, ref_cdf_vals = [], []
            running = 0.0
            for m, p in zip(ref_mids, ref_probs):
                running += p
                if m <= zoom_xmax:
                    ref_cdf_mids.append(m)
                    ref_cdf_vals.append(running / ref_total)
            if ref_cdf_mids:
                ax_cdf.plot(ref_cdf_mids, ref_cdf_vals, linewidth=1.2,
                            color="black", linestyle="--",
                            label="Pure EDCA (combined, n_pedca=0)")
                _add_cdf_percentile_markers(ax_cdf, ref_mids, ref_probs,
                                            zoom_xmax, "black")

    ax_cdf.set_xlim(global_xmin, zoom_xmax)
    ticks = build_ticks(global_xmin, zoom_xmax, fig_width)
    ax_cdf.set_xticks(ticks)
    ax_cdf.set_ylim(0, 1.02)
    ax_cdf.tick_params(axis="x", labelsize=8, rotation=45)
    ax_cdf.set_xlabel("Delay (us)", fontsize=10)
    ax_cdf.set_ylabel("Cumulative Probability", fontsize=11)
    ax_cdf.set_title(
        f"{type_label} Delay CDF  —  nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    ax_cdf.grid(True, alpha=0.25, linestyle="--")
    ax_cdf.legend(loc="lower right", fontsize=8)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


# ─────────── NEW: P-EDCA vs Legacy CDF Comparison (per n_pedca) ─────

def plot_pedca_vs_legacy_cdf_per_count(out_path: Path, data_rate: str,
                                       pedca_counts: list,
                                       n_runs: int = 1,
                                       fig_width: float = 10.0,
                                       fig_height: float = 6.0,
                                       dpi: int = 200):
    """
    For each n_pedca with 0 < n_pedca < N_STA, plot a single CDF figure
    overlaying the P-EDCA-enabled STA CDF and the legacy EDCA STA CDF.
    All pages are bundled into one multi-page PDF at out_path.
    """
    from matplotlib.backends.backend_pdf import PdfPages

    mixed_counts = [c for c in pedca_counts if 0 < c < N_STA]
    if not mixed_counts:
        return None

    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""
    pedca_color  = "#C44E52"   # red-ish
    legacy_color = "#4C72B0"   # blue-ish

    any_written = False
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with PdfPages(str(out_path)) as pdf:
        for n_pedca in mixed_counts:
            pedca_csv  = qsrc_dir() / pedca_sta_csv_name(n_pedca, data_rate)
            legacy_csv = qsrc_dir() / legacy_sta_csv_name(n_pedca, data_rate)

            loaded = {}
            for label_key, csv_path in (("pedca", pedca_csv),
                                         ("legacy", legacy_csv)):
                if not csv_path.exists():
                    continue
                try:
                    mids, probs, xmin, xmax, bw = load_histogram(csv_path)
                except Exception:
                    continue
                loaded[label_key] = (mids, probs, xmin)

            # Try to load combined all-STA CSV at n_pedca=0 as Pure EDCA reference
            edca_ref_csv = qsrc_dir() / csv_name(0, data_rate)
            if edca_ref_csv.exists():
                try:
                    ref_mids, ref_probs, ref_xmin, ref_xmax, ref_bw = load_histogram(edca_ref_csv)
                    loaded["edca_ref"] = (ref_mids, ref_probs, ref_xmin)
                except Exception:
                    pass

            if not loaded:
                continue

            all_series = [(v[0], v[1]) for v in loaded.values()]
            x_95 = _compute_percentile_xlim(all_series, 0.95)
            zoom_xmax = x_95 * 1.10 if x_95 != float("inf") else max(
                max(v[0]) for v in loaded.values()
            )
            global_xmin = min(v[2] for v in loaded.values())

            fig, ax = plt.subplots(figsize=(fig_width, fig_height))

            series_spec = [
                ("pedca",  f"P-EDCA STAs (n={n_pedca})",         pedca_color),
                ("legacy", f"Legacy EDCA STAs (n={N_STA - n_pedca})",
                                                                legacy_color),
                ("edca_ref", "Pure EDCA (all-legacy, n_pedca=0)", "#555555"),
            ]
            plotted = False
            for key, label, color in series_spec:
                if key not in loaded:
                    continue
                mids, probs, _ = loaded[key]
                full_total = sum(probs)
                if full_total <= 0:
                    continue
                cdf_mids, cdf_vals = [], []
                running = 0.0
                for m, p in zip(mids, probs):
                    running += p
                    if m <= zoom_xmax:
                        cdf_mids.append(m)
                        cdf_vals.append(running / full_total)
                if not cdf_mids:
                    continue
                linestyle = "dashed" if key == "edca_ref" else "solid"
                ax.plot(cdf_mids, cdf_vals, linewidth=1.4,
                        color=color, label=label, linestyle=linestyle)
                _add_cdf_percentile_markers(ax, mids, probs, zoom_xmax, color)
                plotted = True

            if not plotted:
                plt.close(fig)
                continue

            ax.set_xlim(global_xmin, zoom_xmax)
            ticks = build_ticks(global_xmin, zoom_xmax, fig_width)
            ax.set_xticks(ticks)
            ax.set_ylim(0, 1.02)
            ax.tick_params(axis="x", labelsize=8, rotation=45)
            ax.set_xlabel("Delay (us)", fontsize=10)
            ax.set_ylabel("Cumulative Probability", fontsize=11)
            ax.set_title(
                f"P-EDCA vs Legacy Delay CDF  —  "
                f"n_pedca={n_pedca}, n_legacy={N_STA - n_pedca}, "
                f"nSta={N_STA}, {data_rate}{runs_label}",
                fontsize=12, fontweight="bold"
            )
            ax.grid(True, alpha=0.3, linestyle="--")
            ax.legend(loc="lower right", fontsize=10)

            fig.tight_layout()
            pdf.savefig(fig, dpi=dpi)
            plt.close(fig)
            any_written = True

    if not any_written:
        try:
            out_path.unlink()
        except OSError:
            pass
        return None
    return out_path


# ─────────── NEW: Computed Metric Plots (Stats 3-7) ─────────────────

def _compute_derived_metrics(stats_path: Path, pedca_counts: list) -> dict:
    """
    Compute all derived metrics (stats 3-7) from the stats file.
    Returns dict of metric_name -> {n_pedca: value}.
    """
    # Load raw counters
    total_pedca_attempt = parse_stats_file_for_metric(stats_path, pedca_counts, "total_pedca_attempt")
    total_pedca_tx = parse_stats_file_for_metric(stats_path, pedca_counts, "total_pedca_tx")
    total_stage2_tx = parse_stats_file_for_metric(stats_path, pedca_counts, "total_stage2_tx")
    fail_rts_no_cts = parse_stats_file_for_metric(stats_path, pedca_counts, "fail_rts_no_cts")
    fail_rts_collision = parse_stats_file_for_metric(stats_path, pedca_counts, "fail_rts_collision")
    fail_timing_expired = parse_stats_file_for_metric(stats_path, pedca_counts, "fail_timing_expired")
    fail_deferral = parse_stats_file_for_metric(stats_path, pedca_counts, "fail_deferral")
    pedca_sta_succ = parse_stats_file_for_metric(stats_path, pedca_counts, "pedca_sta_succ_count")
    pedca_sta_zero = parse_stats_file_for_metric(stats_path, pedca_counts, "pedca_sta_zero_retx")
    legacy_sta_succ = parse_stats_file_for_metric(stats_path, pedca_counts, "legacy_sta_succ_count")
    legacy_sta_zero = parse_stats_file_for_metric(stats_path, pedca_counts, "legacy_sta_zero_retx")

    results = {
        "per_sta_pedca_attempt": {},       # Stat 2: avg DS-CTS per P-EDCA STA
        "pedca_oneshot_success": {},        # Stat 3: pedcaSuccess / dsCts
        "edca_oneshot_success_legacy": {},  # Stat 4: legacy zero-retx ratio
        "edca_oneshot_success_pedca": {},   # Stat 4 ext: pedca-sta zero-retx ratio
        "pedca_kickback_prob": {},          # Stat 5: total failures / dsCts
        "pedca_rts_collision_prob": {},     # Stat 6: collisions / stage2 TX
        "pedca_attempts_per_success": {},   # Stat 7: dsCts / pedcaSuccess
    }

    for n in pedca_counts:
        n_pedca_sta = n
        attempts = total_pedca_attempt.get(n, 0)
        success = total_pedca_tx.get(n, 0)
        s2tx = total_stage2_tx.get(n, 0)
        fail_total = (fail_rts_no_cts.get(n, 0) + fail_rts_collision.get(n, 0)
                      + fail_timing_expired.get(n, 0) + fail_deferral.get(n, 0))
        coll = fail_rts_collision.get(n, 0)

        # Stat 2: per-STA P-EDCA attempts
        if n_pedca_sta > 0:
            results["per_sta_pedca_attempt"][n] = attempts / n_pedca_sta

        # Stat 3: P-EDCA one-shot success
        if n > 0 and attempts > 0:
            results["pedca_oneshot_success"][n] = success / attempts * 100.0

        # Stat 4: EDCA one-shot (legacy)
        ls = legacy_sta_succ.get(n, 0)
        lz = legacy_sta_zero.get(n, 0)
        if ls > 0:
            results["edca_oneshot_success_legacy"][n] = lz / ls * 100.0

        # Stat 4 ext: EDCA one-shot (P-EDCA STAs)
        ps = pedca_sta_succ.get(n, 0)
        pz = pedca_sta_zero.get(n, 0)
        if ps > 0:
            results["edca_oneshot_success_pedca"][n] = pz / ps * 100.0

        # Stat 5: kickback probability
        if n > 0 and attempts > 0:
            results["pedca_kickback_prob"][n] = fail_total / attempts * 100.0

        # Stat 6: RTS collision probability
        if n > 0 and s2tx > 0:
            results["pedca_rts_collision_prob"][n] = coll / s2tx * 100.0

        # Stat 7: attempts per success
        if n > 0 and success > 0:
            results["pedca_attempts_per_success"][n] = attempts / success

    return results


def plot_derived_metric(data: dict, out_path: Path,
                        ylabel: str, title: str,
                        data_rate: str, n_runs: int = 1,
                        color: str = "#4C72B0",
                        fig_width: float = 12.0,
                        fig_height: float = 6.0,
                        dpi: int = 200):
    """Plot a single derived metric vs nPedca."""
    if not data:
        return None

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    runs_label = f" (avg of {n_runs} runs)" if n_runs > 1 else ""

    x_vals = sorted(data.keys())
    y_vals = [data[x] for x in x_vals]

    ax.plot(x_vals, y_vals, marker="o", markersize=5, linewidth=1.5, color=color)
    ax.fill_between(x_vals, y_vals, alpha=0.1, color=color)

    ax.set_xlabel("Number of P-EDCA STAs", fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.set_title(f"{title}  —  nSta={N_STA}, {data_rate}{runs_label}",
                 fontsize=13, fontweight="bold")
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.set_xticks(x_vals)
    ax.tick_params(axis="x", labelsize=8, rotation=45)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


def plot_edca_oneshot_combined(derived: dict, out_path: Path,
                               data_rate: str, n_runs: int = 1,
                               fig_width: float = 12.0,
                               fig_height: float = 6.0,
                               dpi: int = 200):
    """Plot legacy and P-EDCA STA one-shot success on the same axes."""
    legacy = derived.get("edca_oneshot_success_legacy", {})
    pedca = derived.get("edca_oneshot_success_pedca", {})
    if not legacy and not pedca:
        return None

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    runs_label = f" (avg of {n_runs} runs)" if n_runs > 1 else ""

    if legacy:
        x = sorted(legacy.keys())
        y = [legacy[k] for k in x]
        ax.plot(x, y, marker="o", markersize=5, linewidth=1.5,
                color="#C44E52", label="Legacy EDCA STAs")

    if pedca:
        x = sorted(pedca.keys())
        y = [pedca[k] for k in x]
        ax.plot(x, y, marker="s", markersize=5, linewidth=1.5,
                color="#4C72B0", label="P-EDCA STAs")

    ax.set_xlabel("Number of P-EDCA STAs", fontsize=11)
    ax.set_ylabel("One-Shot Success Ratio (%)\n(0 MAC retransmissions)", fontsize=11)
    ax.set_title(
        f"EDCA One-Shot Success Ratio (0 retx)  —  nSta={N_STA}, {data_rate}{runs_label}",
        fontsize=13, fontweight="bold"
    )
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.legend(loc="best", fontsize=10)
    all_x = sorted(set(list(legacy.keys()) + list(pedca.keys())))
    if all_x:
        ax.set_xticks(all_x)
    ax.tick_params(axis="x", labelsize=8, rotation=45)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


def plot_new_stats_combined(derived: dict, stats_path: Path, out_path: Path,
                            data_rate: str, pedca_counts: list,
                            n_runs: int = 1,
                            fig_width: float = 14.0,
                            fig_height: float = 24.0,
                            dpi: int = 200):
    """
    Multi-subplot figure with all new stats (2-7) for quick overview.
    """
    subplots = [
        ("per_sta_pedca_attempt", "Avg P-EDCA Attempts\nper P-EDCA STA",
         "[Stat 2] Per-STA P-EDCA Attempt Count", "#8172B3"),
        ("pedca_oneshot_success", "P-EDCA One-Shot\nSuccess (%)",
         "[Stat 3] P-EDCA One-Shot Success (Success/DS-CTS)", "#55A868"),
        ("edca_oneshot_success_legacy", "Legacy One-Shot\nSuccess (%)",
         "[Stat 4] Legacy EDCA One-Shot (0 retx)", "#C44E52"),
        ("pedca_kickback_prob", "Kickback-to-EDCA\nProbability (%)",
         "[Stat 5] P-EDCA Kickback Probability", "#DD8452"),
        ("pedca_rts_collision_prob", "RTS Collision\nProbability (%)",
         "[Stat 6] P-EDCA RTS Collision Probability", "#d62728"),
        ("pedca_attempts_per_success", "DS-CTS Attempts\nper P-EDCA Success",
         "[Stat 7] P-EDCA Attempts per Success", "#4C72B0"),
    ]

    fig, axes = plt.subplots(len(subplots), 1, figsize=(fig_width, fig_height))
    runs_label = f" (avg of {n_runs} runs)" if n_runs > 1 else ""

    for idx, (key, ylabel, title, color) in enumerate(subplots):
        ax = axes[idx]
        data = derived.get(key, {})
        if data:
            x_vals = sorted(data.keys())
            y_vals = [data[x] for x in x_vals]
            ax.plot(x_vals, y_vals, marker="o", markersize=4, linewidth=1.2, color=color)
            ax.fill_between(x_vals, y_vals, alpha=0.08, color=color)
            ax.set_xticks(x_vals)
        ax.set_xlabel("Number of P-EDCA STAs", fontsize=9)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.set_title(f"{title}  —  nSta={N_STA}, {data_rate}{runs_label}",
                     fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.3, linestyle="--")
        ax.tick_params(axis="x", labelsize=7, rotation=45)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


# ─────────────────── QSRC-Overlay Plot (across QSRC) ────────────────

def _series_csv_for(sta_type: str, qsrc: int, n_pedca: int,
                    data_rate: str) -> Path:
    """Pick the right CSV path for a given STA type and (qsrc, n_pedca)."""
    set_current_qsrc(qsrc)
    if sta_type == "all":
        return qsrc_dir() / csv_name(n_pedca, data_rate)
    elif sta_type == "pedca":
        return qsrc_dir() / pedca_sta_csv_name(n_pedca, data_rate)
    elif sta_type == "legacy":
        return qsrc_dir() / legacy_sta_csv_name(n_pedca, data_rate)
    raise ValueError(sta_type)


def plot_qsrc_overlay_for_npedca(out_path: Path, data_rate: str,
                                  n_pedca: int, qsrc_values: list,
                                  sta_type: str, n_runs: int = 1,
                                  fig_width: float = 14.0,
                                  fig_height: float = 10.0,
                                  dpi: int = 200):
    """
    Build a single figure (PDF + CDF stacked) overlaying curves for
    QSRC=1..10 at a fixed n_pedca. sta_type ∈ {'all','pedca','legacy'}.
    """
    type_label = {
        "all": "All STAs (VO)",
        "pedca": "P-EDCA STAs",
        "legacy": "Legacy EDCA STAs",
    }[sta_type]

    # High-contrast palette ordered roughly red→orange→yellow→green→cyan→blue→purple.
    # Designed for up to 10 discrete QSRC curves to stay visually distinct.
    QSRC_PALETTE = [
        "#e41a1c",  # red
        "#ff7f00",  # orange
        "#ffdd00",  # yellow
        "#4daf4a",  # green
        "#00ced1",  # cyan / dark turquoise
        "#377eb8",  # blue
        "#984ea3",  # purple
        "#f781bf",  # pink
        "#a65628",  # brown
        "#000000",  # black
    ]
    colors = {q: QSRC_PALETTE[i % len(QSRC_PALETTE)]
              for i, q in enumerate(qsrc_values)}

    loaded = {}
    all_series = []
    global_xmin = float("inf")
    for q in qsrc_values:
        csv_path = _series_csv_for(sta_type, q, n_pedca, data_rate)
        if not csv_path.exists():
            continue
        try:
            mids, probs, xmin, xmax, bw = load_histogram(csv_path)
        except Exception:
            continue
        loaded[q] = (mids, probs, bw)
        all_series.append((mids, probs))
        global_xmin = min(global_xmin, xmin)

    if not loaded:
        return None

    x_95 = _compute_percentile_xlim(all_series, 0.95)
    zoom_xmax = x_95 * 1.10 if x_95 != float("inf") else max(
        max(v[0]) for v in loaded.values()
    )

    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""

    fig, (ax_pdf, ax_cdf) = plt.subplots(2, 1, figsize=(fig_width, fig_height))

    # PDF
    for q in qsrc_values:
        if q not in loaded:
            continue
        mids, probs, _ = loaded[q]
        z_mids = [m for m in mids if m <= zoom_xmax]
        z_probs = [p for m, p in zip(mids, probs) if m <= zoom_xmax]
        if not z_mids:
            continue
        ax_pdf.plot(z_mids, z_probs, linewidth=0.9, color=colors[q],
                    label=f"QSRC={q}")
        ax_pdf.fill_between(z_mids, z_probs, alpha=0.06, color=colors[q])

    ax_pdf.set_xlim(global_xmin, zoom_xmax)
    ticks = build_ticks(global_xmin, zoom_xmax, fig_width)
    ax_pdf.set_xticks(ticks)
    ax_pdf.tick_params(axis="x", labelsize=8, rotation=45)
    ax_pdf.set_xlabel("Delay (µs)", fontsize=10)
    ax_pdf.set_ylabel("Probability", fontsize=11)
    ax_pdf.set_title(
        f"{type_label} Delay PDF (zoom 95th pctl)  —  "
        f"n_pedca={n_pedca}/{N_STA}, {data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    ax_pdf.grid(True, alpha=0.25, linestyle="--")
    ax_pdf.legend(loc="upper right", fontsize=8, ncol=2)

    # CDF
    for q in qsrc_values:
        if q not in loaded:
            continue
        mids, probs, _ = loaded[q]
        full_total = sum(probs)
        if full_total <= 0:
            continue
        cdf_mids, cdf_vals = [], []
        running = 0.0
        for m, p in zip(mids, probs):
            running += p
            if m <= zoom_xmax:
                cdf_mids.append(m)
                cdf_vals.append(running / full_total)
        if not cdf_mids:
            continue
        ax_cdf.plot(cdf_mids, cdf_vals, linewidth=1.1, color=colors[q],
                    label=f"QSRC={q}")
        _add_cdf_percentile_markers(ax_cdf, mids, probs, zoom_xmax, colors[q])

    ax_cdf.set_xlim(global_xmin, zoom_xmax)
    ax_cdf.set_xticks(ticks)
    ax_cdf.set_ylim(0, 1.02)
    ax_cdf.tick_params(axis="x", labelsize=8, rotation=45)
    ax_cdf.set_xlabel("Delay (µs)", fontsize=10)
    ax_cdf.set_ylabel("Cumulative Probability", fontsize=11)
    ax_cdf.set_title(
        f"{type_label} Delay CDF  —  "
        f"n_pedca={n_pedca}/{N_STA}, {data_rate}{runs_label}",
        fontsize=12, fontweight="bold"
    )
    ax_cdf.grid(True, alpha=0.25, linestyle="--")
    ax_cdf.legend(loc="lower right", fontsize=8, ncol=2)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    plt.close(fig)
    return out_path


# ─────────── EDCA-only Baseline + Per-Combo CDF (the deliverable) ───────────

# (delay_type key, human label, csv-name function)
DELAY_TYPES = [
    ("vo",     "VO (all STAs)",  csv_name),
    ("pedca",  "P-EDCA STA VO",  pedca_sta_csv_name),
    ("legacy", "Legacy STA VO",  legacy_sta_csv_name),
]

# Accumulates one row per (combo, nPedca, delay_type) for a final summary CSV.
PCTL_SUMMARY_ROWS = []


def run_baseline(data_rate, sim_time, bin_us, n_runs, workers):
    """Run the EDCA-only baseline (pedcaRatio=0) ONCE. QSRC/PSRC are irrelevant
    when no STA runs P-EDCA, so a single run against the current binary suffices.
    Returns the averaged VO-delay histogram (mids, probs) overlaid as the
    'EDCA only' reference on every combo CDF."""
    set_baseline_mode(True)
    out_dir = qsrc_dir()        # -> OUT_DIR/edca_only
    print(f"\n┌── EDCA-only baseline (pedcaRatio=0, {n_runs} runs) ──┐  out={out_dir}")
    raw = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_single_sim, BASELINE_NPEDCA, data_rate,
                          sim_time, bin_us, r) for r in range(n_runs)]
        for fut in as_completed(futs):
            raw.append(fut.result())
    agg = aggregate_runs(raw, BASELINE_NPEDCA, data_rate, n_runs)
    write_comparison_stats({BASELINE_NPEDCA: agg}, data_rate, n_runs, [BASELINE_NPEDCA])
    base_curve = None
    if agg["csv_path"] and Path(agg["csv_path"]).exists():
        try:
            mids, probs, *_ = load_histogram(Path(agg["csv_path"]))
            base_curve = (mids, probs)
        except Exception as e:
            print(f"  ⚠ baseline histogram load failed: {e}")
    set_baseline_mode(False)
    print(f"  {'✔' if base_curve else '⚠'} baseline VO histogram ready "
          f"({agg.get('n_success', 0)}/{n_runs} runs)")
    return base_curve


def load_baseline_curve(data_rate):
    """Reload a previously-computed baseline VO histogram (used by --plot-only)."""
    set_baseline_mode(True)
    p = qsrc_dir() / csv_name(BASELINE_NPEDCA, data_rate)
    set_baseline_mode(False)
    if p.exists():
        try:
            mids, probs, *_ = load_histogram(p)
            return (mids, probs)
        except Exception:
            return None
    return None


def plot_combo_cdf(delay_type, type_label, csv_fn, n_pedca, data_rate,
                   cwds, qsrc, psrc, baseline_curve, n_runs,
                   fig_width=12.0, fig_height=6.0, dpi=200):
    """One CDF figure for a single (CWds,QSRC,PSRC) combo, fixed nPedca and one
    delay distribution. Overlays the EDCA-only baseline and marks the combo's
    median (P50) and P95. Appends a percentile row to PCTL_SUMMARY_ROWS."""
    combo_csv = qsrc_dir() / csv_fn(n_pedca, data_rate)
    if not combo_csv.exists():
        return None
    try:
        mids, probs, xmin, xmax, bw = load_histogram(combo_csv)
    except Exception:
        return None
    if sum(probs) <= 0:
        return None

    series = [(mids, probs)]
    if baseline_curve:
        series.append(baseline_curve)
    zoom_xmax = max(_compute_percentile_xlim(series, 0.95) * 1.15, bw * 4)
    gmin = mids[0]
    if baseline_curve and baseline_curve[0]:
        gmin = min(gmin, baseline_curve[0][0])

    pcts = compute_percentiles_from_histogram(mids, probs, [0.5, 0.95, 0.99])

    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    def _cdf(m_list, p_list):
        tot = sum(p_list)
        cm, cv, run = [], [], 0.0
        if tot <= 0:
            return cm, cv
        for m, p in zip(m_list, p_list):
            run += p
            if m <= zoom_xmax:
                cm.append(m); cv.append(run / tot)
        return cm, cv

    # EDCA-only baseline first (combo drawn on top)
    if baseline_curve:
        bcm, bcv = _cdf(baseline_curve[0], baseline_curve[1])
        if bcm:
            ax.plot(bcm, bcv, linewidth=1.3, color="#888888",
                    linestyle="--", label="EDCA only")

    ccm, ccv = _cdf(mids, probs)
    ax.plot(ccm, ccv, linewidth=1.8, color="#C44E52",
            label=f"CWds={cwds} × QSRC={qsrc} × PSRC={psrc}")

    # median + p95 vertical markers, annotated with the value
    for lvl, col, name in [(0.5, "#4C72B0", "P50"), (0.95, "#DD8452", "P95")]:
        xv = pcts[lvl]
        if xv <= zoom_xmax:
            ax.axvline(xv, color=col, linewidth=1.0, linestyle=":")
            ax.annotate(f"{name}={xv:.0f} µs", xy=(xv, lvl),
                        xytext=(4, 4 if lvl == 0.5 else -12),
                        textcoords="offset points",
                        fontsize=8, color=col, fontweight="bold")

    ax.set_xlim(gmin, zoom_xmax)
    ax.set_xticks(build_ticks(gmin, zoom_xmax, fig_width))
    ax.set_ylim(0, 1.02)
    ax.tick_params(axis="x", labelsize=8, rotation=45)
    ax.set_xlabel("Delay (µs)", fontsize=10)
    ax.set_ylabel("Cumulative Probability", fontsize=11)
    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""
    ax.set_title(f"{type_label} Delay CDF — CWds={cwds} × QSRC={qsrc} × PSRC={psrc}\n"
                 f"nSta={N_STA}, nPedca={n_pedca}, {data_rate}{runs_label}",
                 fontsize=12, fontweight="bold")
    ax.grid(True, alpha=0.25, linestyle="--")
    ax.legend(loc="lower right", fontsize=9)

    fig.tight_layout()
    out = qsrc_dir() / (f"{combo_tag(cwds, qsrc, psrc)}_{delay_type}_cdf_"
                        f"p{n_pedca:02d}_{data_rate}.pdf")
    fig.savefig(str(out), dpi=dpi)
    plt.close(fig)

    PCTL_SUMMARY_ROWS.append({
        "cwds": cwds, "qsrc": qsrc, "psrc": psrc, "n_pedca": n_pedca,
        "delay_type": delay_type,
        "p50_us": pcts[0.5], "p95_us": pcts[0.95], "p99_us": pcts[0.99],
    })
    return pcts


def plot_combo_cdf_three_types(n_pedca, data_rate, cwds, qsrc, psrc,
                               baseline_curve, n_runs,
                               fig_width=12.0, fig_height=7.0, dpi=200):
    """
    One CDF figure overlaying all three STA-type distributions
    (All STAs / P-EDCA STAs / Legacy STAs) plus the EDCA-only baseline.
    Vertical dashed lines mark P50 / P95 / P99 for each curve.
    Appends percentile rows for all three types to PCTL_SUMMARY_ROWS.
    """
    # --- load the three histograms ---
    loaded = {}
    for delay_type, _label, csv_fn in DELAY_TYPES:
        p = qsrc_dir() / csv_fn(n_pedca, data_rate)
        if not p.exists():
            continue
        try:
            mids, probs, xmin, xmax, bw = load_histogram(p)
            if sum(probs) > 0:
                loaded[delay_type] = (mids, probs, bw)
        except Exception:
            pass

    if not loaded:
        return None

    # colour / style per type  (solid for the P-EDCA/legacy types, thicker for all)
    style_map = {
        "vo":     ("#C44E52", "-",  2.0, "All STAs (VO)"),
        "pedca":  ("#4C72B0", "-",  1.6, "P-EDCA STAs"),
        "legacy": ("#55A868", "--", 1.6, "Legacy STAs"),
    }

    # compute shared x-axis limits across all loaded series + baseline
    all_series = [(m, p) for m, p, _ in loaded.values()]
    if baseline_curve:
        all_series.append(baseline_curve)
    zoom_xmax = max(_compute_percentile_xlim(all_series, 0.95) * 1.15, 1.0)
    gmin = min(m[0] for m, _, _ in loaded.values())
    if baseline_curve and baseline_curve[0]:
        gmin = min(gmin, baseline_curve[0][0])

    runs_label = f", avg of {n_runs} runs" if n_runs > 1 else ""
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    def _cdf(mids, probs):
        tot = sum(probs)
        cm, cv, run = [], [], 0.0
        if tot <= 0:
            return cm, cv
        for m, p in zip(mids, probs):
            run += p
            if m <= zoom_xmax:
                cm.append(m); cv.append(run / tot)
        return cm, cv

    # EDCA-only baseline (grey dashed, drawn first so it's behind)
    if baseline_curve:
        bcm, bcv = _cdf(baseline_curve[0], baseline_curve[1])
        if bcm:
            ax.plot(bcm, bcv, linewidth=1.3, color="#888888",
                    linestyle="--", label="EDCA only (baseline)")

    # Draw each loaded type and mark percentiles
    pct_y_offsets = {"vo": 0.60, "pedca": 0.40, "legacy": 0.20}
    for delay_type in ["vo", "pedca", "legacy"]:
        if delay_type not in loaded:
            continue
        mids, probs, _ = loaded[delay_type]
        color, ls, lw, lbl = style_map[delay_type]
        cm, cv = _cdf(mids, probs)
        if not cm:
            continue
        ax.plot(cm, cv, linewidth=lw, color=color, linestyle=ls, label=lbl)

        pcts = compute_percentiles_from_histogram(mids, probs, [0.5, 0.95, 0.99])
        PCTL_SUMMARY_ROWS.append({
            "cwds": cwds, "qsrc": qsrc, "psrc": psrc, "n_pedca": n_pedca,
            "delay_type": delay_type,
            "p50_us": pcts[0.5], "p95_us": pcts[0.95], "p99_us": pcts[0.99],
        })

        y_base = pct_y_offsets.get(delay_type, 0.5)
        for i, (lvl, name) in enumerate([(0.5, "P50"), (0.95, "P95"), (0.99, "P99")]):
            xv = pcts[lvl]
            if xv <= zoom_xmax:
                ax.axvline(xv, color=color, linewidth=0.7, linestyle=":",
                           alpha=0.70)
                ax.text(xv + zoom_xmax * 0.005, y_base + i * 0.07,
                        f"{name}={xv:.0f}µs",
                        fontsize=6.5, color=color, va="bottom")

    ax.set_xlim(gmin, zoom_xmax)
    ax.set_xticks(build_ticks(gmin, zoom_xmax, fig_width))
    ax.set_ylim(0, 1.02)
    ax.tick_params(axis="x", labelsize=8, rotation=45)
    ax.set_xlabel("Delay (µs)", fontsize=10)
    ax.set_ylabel("Cumulative Probability", fontsize=11)
    ax.set_title(
        f"VO Delay CDF — P-EDCA / Legacy / All  |  "
        f"CWds={cwds} × QSRC={qsrc} × PSRC={psrc}\n"
        f"nSta={N_STA}, nPedca={n_pedca}/{N_STA}, {data_rate}{runs_label}",
        fontsize=11, fontweight="bold"
    )
    ax.grid(True, alpha=0.25, linestyle="--")
    ax.legend(loc="lower right", fontsize=9)

    fig.tight_layout()
    out = qsrc_dir() / (f"{combo_tag(cwds, qsrc, psrc)}_3type_cdf_"
                        f"p{n_pedca:02d}_{data_rate}.pdf")
    fig.savefig(str(out), dpi=dpi)
    plt.close(fig)
    return out


def generate_combo_cdfs(cwds, qsrc, psrc, baseline_curve, data_rate,
                        pedca_counts, n_runs, args):
    """Per-combo deliverable: for each nPedca, emit:
      - 3 separate CDF figures (one per delay type: all / pedca / legacy)
      - 1 combined figure overlaying all three types on the same axes
    All figures include the EDCA-only baseline for comparison."""
    set_current_cwds(cwds)
    set_current_qsrc(qsrc)
    set_current_psrc(psrc)
    n = 0
    for n_pedca in pedca_counts:
        # Individual per-type CDFs (existing behaviour)
        for delay_type, type_label, csv_fn in DELAY_TYPES:
            r = plot_combo_cdf(delay_type, type_label, csv_fn, n_pedca,
                               data_rate, cwds, qsrc, psrc, baseline_curve,
                               n_runs, args.fig_width, args.fig_height, args.dpi)
            if r:
                n += 1
        # New: combined 3-type CDF
        r = plot_combo_cdf_three_types(n_pedca, data_rate, cwds, qsrc, psrc,
                                       baseline_curve, n_runs,
                                       args.fig_width, args.fig_height + 1.0,
                                       args.dpi)
        if r:
            n += 1
    print(f"    ✔ {n} per-combo CDF figures for CWds={cwds} × QSRC={qsrc} × PSRC={psrc}")


def write_pctl_summary(data_rate):
    """Write the median/P95/P99 of every (combo, nPedca, delay_type) to one CSV."""
    if not PCTL_SUMMARY_ROWS:
        return None
    out = OUT_DIR / f"combo_percentile_summary_{data_rate}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["CWds", "QSRC", "PSRC", "nPedca", "delay_type",
                    "P50_us", "P95_us", "P99_us"])
        for r in sorted(PCTL_SUMMARY_ROWS,
                        key=lambda r: (r["delay_type"], r["n_pedca"],
                                       r["cwds"], r["qsrc"], r["psrc"])):
            w.writerow([r["cwds"], r["qsrc"], r["psrc"], r["n_pedca"],
                        r["delay_type"], f"{r['p50_us']:.2f}",
                        f"{r['p95_us']:.2f}", f"{r['p99_us']:.2f}"])
    return out


# ──────────────────────────── Main ───────────────────────────────────

def _combo_is_complete(cwds: int, qsrc: int, psrc: int,
                       pedca_counts: list, data_rate: str) -> bool:
    """Return True if all averaged VO-delay CSVs already exist for this combo."""
    set_current_cwds(cwds); set_current_qsrc(qsrc); set_current_psrc(psrc)
    d = qsrc_dir()
    return all((d / csv_name(n, data_rate)).exists() for n in pedca_counts)


def run_one_combo(cwds: int, qsrc: int, psrc: int, baseline_curve, data_rate: str,
                  sim_time: float, bin_us: int,
                  pedca_counts: list, n_runs: int, workers: int, args):
    """Run all nPedca points for a fixed (CWds,QSRC,PSRC) combo, write stats, and
    generate both the existing per-config metric plots and the per-combo CDFs.
    CWds is applied at runtime via --cwds (no rebuild); QSRC/PSRC are baked into
    the binary already built by the caller."""
    set_current_cwds(cwds)
    set_current_qsrc(qsrc)
    set_current_psrc(psrc)
    out_dir = qsrc_dir()

    # --skip-existing: if all averaged CSVs are present, skip simulation only
    skip_existing = getattr(args, "skip_existing", False)
    if skip_existing and _combo_is_complete(cwds, qsrc, psrc, pedca_counts, data_rate):
        print(f"\n  [skip] CWds={cwds} × QSRC={qsrc} × PSRC={psrc} — data complete, generating plots only")
        stats_path = (out_dir / f"pedca_count_sweep_statistics_{qsrc_tag()}_{data_rate}.txt")
        if stats_path.exists():
            generate_per_qsrc_plots(stats_path, data_rate, pedca_counts, n_runs, args)
        generate_combo_cdfs(cwds, qsrc, psrc, baseline_curve, data_rate,
                            pedca_counts, n_runs, args)
        return

    print(f"\n┌──────────────────────────────────────────────────────────┐")
    print(f"│  CWds = {cwds} × QSRC = {qsrc:>2} × PSRC = {psrc:>2}")
    print(f"│  out dir = {out_dir}")
    print(f"└──────────────────────────────────────────────────────────┘")

    results = {}
    total_sims = len(pedca_counts) * n_runs
    print(f"  Launching {len(pedca_counts)} configs × {n_runs} runs = {total_sims} sims  "
          f"({workers} concurrent)")

    raw_results = defaultdict(list)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for n_pedca in pedca_counts:
            for run_idx in range(n_runs):
                fut = executor.submit(
                    run_single_sim, n_pedca, data_rate,
                    sim_time, bin_us, run_idx
                )
                futures[fut] = (n_pedca, run_idx)

        done_count = 0
        for future in as_completed(futures):
            n_pedca, run_idx = futures[future]
            done_count += 1
            try:
                r = future.result()
                raw_results[n_pedca].append(r)
                status = "✔" if r["success"] else "✗"
                if done_count % max(1, total_sims // 40) == 0 or not r["success"]:
                    print(f"  [q={qsrc} {done_count}/{total_sims}] {status} "
                          f"nPedca={n_pedca:>2} run={run_idx} {r['elapsed']:.1f}s")
            except Exception as e:
                print(f"  ✗ q={qsrc} nPedca={n_pedca} run={run_idx} EXCEPTION: {e}")

    for n_pedca in sorted(raw_results.keys()):
        run_list = raw_results[n_pedca]
        if run_list:
            agg = aggregate_runs(run_list, n_pedca, data_rate, n_runs)
            results[n_pedca] = agg
            status = "✔" if agg["success"] else "✗"
            print(f"  {status} q={qsrc} nPedca={n_pedca:>2}  "
                  f"{agg['n_success']}/{n_runs} runs OK  "
                  f"elapsed={agg['elapsed']:.1f}s")

    write_log(data_rate, results, n_runs, pedca_counts)
    stats_path = write_comparison_stats(results, data_rate, n_runs, pedca_counts)
    print(f"    ✔ {stats_path.name}  ({stats_path.stat().st_size:,} bytes)")

    # Existing per-config metric plots (kept) into the per-combo subdir
    generate_per_qsrc_plots(stats_path, data_rate, pedca_counts, n_runs, args)
    # NEW: per-combo CDF deliverable (3 delay types × nPedca, with EDCA-only baseline)
    generate_combo_cdfs(cwds, qsrc, psrc, baseline_curve, data_rate,
                        pedca_counts, n_runs, args)
    return stats_path


def generate_per_qsrc_plots(stats_path: Path, data_rate: str,
                            pedca_counts: list, n_runs: int, args):
    """Per-QSRC plots (metrics-vs-nPedca, overlays). Writes into qsrc_dir()."""
    out_dir = qsrc_dir()
    fw, fh, dpi = args.fig_width, args.fig_height, args.dpi
    q = qsrc_tag()

    combined_pdf = out_dir / f"{q}_combined_metrics_vs_pedca_count_{data_rate}.pdf"
    plot_combined_metrics(stats_path, combined_pdf, data_rate, pedca_counts,
                          n_runs, fw, 28.0, dpi)

    individual_plots = [
        ("vo_packet_loss", "VO Packet Loss (%)", "VO Packet Loss vs P-EDCA STAs",
         f"{q}_vo_packet_loss_vs_pedca_count_{data_rate}.pdf"),
        ("channel_idle", "Channel Idle Time (%)", "Channel Idle Time vs P-EDCA STAs",
         f"{q}_channel_idle_vs_pedca_count_{data_rate}.pdf"),
        ("pedca_tx_ratio", "Per-STA P-EDCA Tx Ratio\n(pedcaTx / edcaTx)",
         "Per-P-EDCA-STA: pedcaTx / edcaTx",
         f"{q}_pedca_tx_ratio_vs_pedca_count_{data_rate}.pdf"),
        ("pedca_success_rate", "P-EDCA Success Rate\n(pedcaTx / pedcaAttempt)",
         "P-EDCA Success Rate: pedcaTx / pedcaAttempt",
         f"{q}_pedca_success_rate_vs_pedca_count_{data_rate}.pdf"),
        ("vo_throughput", "VO Throughput (Mbps)", "VO Throughput vs P-EDCA STAs",
         f"{q}_vo_throughput_vs_pedca_count_{data_rate}.pdf"),
        ("vo_mac_delay", "VO Avg MAC Delay (µs)", "VO Avg MAC Delay vs P-EDCA STAs",
         f"{q}_vo_mac_delay_vs_pedca_count_{data_rate}.pdf"),
    ]
    for metric, ylabel, title, filename in individual_plots:
        plot_metric_vs_pedca_count(stats_path, out_dir / filename,
                                   metric, ylabel, title, data_rate,
                                   pedca_counts, n_runs, fw, fh, dpi)

    plot_pedca_success_share(stats_path,
        out_dir / f"{q}_pedca_success_share_vs_pedca_count_{data_rate}.pdf",
        data_rate, pedca_counts, n_runs, fw, fh, dpi)

    plot_delay_pdf_overlay({},
        out_dir / f"{q}_vo_delay_pdf_cdf_overlay_{data_rate}.pdf",
        data_rate, pedca_counts, n_runs, fw, 10.0, dpi)

    plot_sta_type_delay_overlay(
        out_dir / f"{q}_legacy_sta_delay_pdf_cdf_overlay_{data_rate}.pdf",
        data_rate, pedca_counts, "legacy", n_runs, fw, 10.0, dpi)

    plot_sta_type_delay_overlay(
        out_dir / f"{q}_pedca_sta_delay_pdf_cdf_overlay_{data_rate}.pdf",
        data_rate, pedca_counts, "pedca", n_runs, fw, 10.0, dpi)

    plot_pedca_vs_legacy_cdf_per_count(
        out_dir / f"{q}_pedca_vs_legacy_cdf_per_count_{data_rate}.pdf",
        data_rate, pedca_counts, n_runs, fw, 6.5, dpi)

    derived = _compute_derived_metrics(stats_path, pedca_counts)
    plot_derived_metric(derived["per_sta_pedca_attempt"],
        out_dir / f"{q}_per_sta_pedca_attempt_vs_pedca_count_{data_rate}.pdf",
        "Avg P-EDCA Attempts\nper P-EDCA STA",
        "[Stat 2] Per-STA P-EDCA Attempts",
        data_rate, n_runs, "#8172B3", fw, fh, dpi)
    plot_derived_metric(derived["pedca_oneshot_success"],
        out_dir / f"{q}_pedca_oneshot_success_vs_pedca_count_{data_rate}.pdf",
        "P-EDCA One-Shot\nSuccess (%)",
        "[Stat 3] P-EDCA One-Shot Success (Success/DS-CTS)",
        data_rate, n_runs, "#55A868", fw, fh, dpi)
    plot_edca_oneshot_combined(derived,
        out_dir / f"{q}_edca_oneshot_success_vs_pedca_count_{data_rate}.pdf",
        data_rate, n_runs, fw, fh, dpi)
    plot_derived_metric(derived["pedca_kickback_prob"],
        out_dir / f"{q}_pedca_kickback_prob_vs_pedca_count_{data_rate}.pdf",
        "Kickback-to-EDCA\nProbability (%)",
        "[Stat 5] P-EDCA Kickback Probability",
        data_rate, n_runs, "#DD8452", fw, fh, dpi)
    plot_derived_metric(derived["pedca_rts_collision_prob"],
        out_dir / f"{q}_pedca_rts_collision_prob_vs_pedca_count_{data_rate}.pdf",
        "RTS Collision\nProbability (%)",
        "[Stat 6] P-EDCA RTS Collision Probability",
        data_rate, n_runs, "#d62728", fw, fh, dpi)
    plot_derived_metric(derived["pedca_attempts_per_success"],
        out_dir / f"{q}_pedca_attempts_per_success_vs_pedca_count_{data_rate}.pdf",
        "DS-CTS Attempts\nper P-EDCA Success",
        "[Stat 7] P-EDCA Attempts per Success",
        data_rate, n_runs, "#4C72B0", fw, fh, dpi)
    plot_new_stats_combined(derived, stats_path,
        out_dir / f"{q}_new_stats_combined_vs_pedca_count_{data_rate}.pdf",
        data_rate, pedca_counts, n_runs, fw, 24.0, dpi)


def main():
    parser = argparse.ArgumentParser(
        description="P-EDCA CWds×QSRC×PSRC joint sweep: fixed nSta=30, "
                    "per-combo CDFs at nPedca∈{5,15,30} with EDCA-only baseline"
    )
    parser.add_argument("--plot-only", action="store_true",
                        help="Skip simulations + builds, only re-plot existing data")
    parser.add_argument("--skip-rebuild", action="store_true",
                        help="Skip ns3 builds (assume binary already matches QSRC/PSRC)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip simulation for combos whose averaged CSVs already exist "
                             "(re-generates plots only); useful for resuming a crashed sweep")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help=f"Max parallel workers (default: {MAX_WORKERS})")
    parser.add_argument("--runs", type=int, default=N_RUNS,
                        help=f"Runs per scenario to average (default: {N_RUNS})")
    parser.add_argument("--data-rate", type=str, default=DATA_RATE,
                        help=f"Per-STA VO data rate, e.g. 0.1Mbps / 0.5Mbps "
                             f"(default: {DATA_RATE}). Output files are tagged with "
                             f"this value, so multiple rates coexist in one dir.")
    parser.add_argument("--counts", nargs="+", type=int, default=None,
                        help=f"P-EDCA STA counts to run each combo at (default: {PEDCA_COUNTS})")
    parser.add_argument("--cwds-values", nargs="+", type=int, default=None,
                        help=f"CWds values to sweep (runtime --cwds, default: {CWDS_VALUES})")
    parser.add_argument("--qsrc-values", nargs="+", type=int, default=None,
                        help=f"QSRC threshold values to sweep (default: {QSRC_VALUES})")
    parser.add_argument("--psrc-values", nargs="+", type=int, default=None,
                        help=f"PSRC limit values to sweep (default: {PSRC_VALUES})")
    parser.add_argument("--fig-width",  type=float, default=14.0)
    parser.add_argument("--fig-height", type=float, default=6.0)
    parser.add_argument("--dpi",        type=int,   default=200)
    args = parser.parse_args()

    data_rate = args.data_rate
    sim_time  = SIM_TIME
    bin_us    = BIN_WIDTH
    workers   = args.workers
    n_runs    = args.runs
    pedca_counts = sorted(args.counts) if args.counts else list(PEDCA_COUNTS)
    cwds_values  = sorted(args.cwds_values) if args.cwds_values else list(CWDS_VALUES)
    qsrc_values  = sorted(args.qsrc_values) if args.qsrc_values else list(QSRC_VALUES)
    psrc_values  = sorted(args.psrc_values) if args.psrc_values else list(PSRC_VALUES)
    # CWds, QSRC, PSRC are ALL runtime args now — only ONE build needed for the whole sweep.
    combos = [(c, q, s) for c in cwds_values for q in qsrc_values for s in psrc_values]

    print(f"\n╔══════════════════════════════════════════════════════════╗")
    print(f"║  P-EDCA CWds×QSRC×PSRC Joint Sweep (Fixed nSta={N_STA})")
    print(f"║  CWds values   = {cwds_values}")
    print(f"║  QSRC values   = {qsrc_values}")
    print(f"║  PSRC values   = {psrc_values}")
    print(f"║  → {len(combos)} combos (1 build total), each at nPedca = {pedca_counts}")
    print(f"║  EDCA-only baseline: nPedca={BASELINE_NPEDCA} (run once)")
    print(f"║  dataRate = {data_rate}    simTime = {sim_time}s")
    print(f"║  runs = {n_runs}    workers = {workers}")
    print(f"║  binary = {SIM_BINARY}")
    print(f"╚══════════════════════════════════════════════════════════╝\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t_total = time.time()

    base_curve = None
    if args.plot_only:
        base_curve = load_baseline_curve(data_rate)
        print(f"  [plot-only] baseline curve: "
              f"{'loaded' if base_curve else 'MISSING'}")
        for (cwds, qsrc, psrc) in combos:
            set_current_cwds(cwds)
            set_current_qsrc(qsrc)
            set_current_psrc(psrc)
            stats_path = (qsrc_dir() /
                f"pedca_count_sweep_statistics_{qsrc_tag()}_{data_rate}.txt")
            if stats_path.exists():
                generate_per_qsrc_plots(stats_path, data_rate,
                                        pedca_counts, n_runs, args)
            generate_combo_cdfs(cwds, qsrc, psrc, base_curve, data_rate,
                                pedca_counts, n_runs, args)
    else:
        # One build covers the whole sweep (all params are runtime now).
        if not args.skip_rebuild:
            rebuild_ns3()
        base_curve = run_baseline(data_rate, sim_time, bin_us, n_runs, workers)

        for (cwds, qsrc, psrc) in combos:
            run_one_combo(cwds, qsrc, psrc, base_curve, data_rate,
                          sim_time, bin_us, pedca_counts, n_runs,
                          workers, args)

    # ── Percentile summary across every combo ──
    summary = write_pctl_summary(data_rate)

    elapsed_total = time.time() - t_total
    print(f"\n{'═'*60}")
    print(f"  Sweep complete!  Total time: {elapsed_total:.1f}s")
    print(f"  Output directory: {OUT_DIR}")
    if summary:
        print(f"  Percentile summary: {summary.name}")
    print(f"\n  Per-combo subdirectories:")
    for (cwds, qsrc, psrc) in combos:
        d = OUT_DIR / combo_tag(cwds, qsrc, psrc)
        if d.exists():
            n_cdf = len(list(d.glob("*_cdf_p*.pdf")))
            n_pdf = len(list(d.glob("*.pdf")))
            print(f"    {d.name}/  ({n_pdf} pdfs, {n_cdf} combo-CDFs)")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
