#!/usr/bin/env python3
"""Analyze the paired-seed On/Off saturation comparison.

Delay percentiles are for delivered packets.  R(10 ms) combines the delivered-packet CDF
with WifiTxStatsHelper's completed-MPDU loss, matching the loss-aware metric used to choose
the offline best in traffic_model_comparison_ontime.*.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
SEEDS = (1, 2, 3)
KS = (5, 15, 30)
ARMS = ("edca", "default", "best", "adaptive")
LABEL = {
    "edca": "EDCA",
    "default": "offline default",
    "best": "offline best",
    "adaptive": "burst-adaptive",
}
PARAM = {
    "edca": "--",
    "default": "(0,2,1)",
    "best": "(0,4,3)",
    "adaptive": "dynamic",
}
COLOR = {
    "edca": "#6b7280",
    "default": "#d97706",
    "best": "#2563eb",
    "adaptive": "#dc2626",
}


def stem(arm: str, k: int, seed: int) -> Path:
    if arm == "edca":
        name = f"edca_k0_s{seed}"
    elif arm == "adaptive":
        name = f"adaptive3_k{k}_s{seed}" if k == 30 else f"adaptive2_k{k}_s{seed}"
    else:
        name = f"{arm}_k{k}_s{seed}"
    return HERE / name


def load_hist(path: Path) -> tuple[np.ndarray, np.ndarray]:
    bins: dict[float, int] = defaultdict(int)
    with path.open() as f:
        for row in csv.DictReader(f):
            bins[float(row["bin_mid_us"])] += int(float(row["count"]))
    mids = np.asarray(sorted(bins))
    counts = np.asarray([bins[x] for x in mids], dtype=float)
    if counts.sum() == 0:
        raise ValueError(f"empty histogram: {path}")
    return mids, counts


def pool_hist(arm: str, k: int) -> tuple[np.ndarray, np.ndarray]:
    bins: dict[float, int] = defaultdict(int)
    for seed in SEEDS:
        mids, counts = load_hist(stem(arm, k, seed).with_suffix(".csv"))
        for mid, count in zip(mids, counts):
            bins[float(mid)] += int(count)
    mids = np.asarray(sorted(bins))
    return mids, np.asarray([bins[x] for x in mids], dtype=float)


def percentile(mids: np.ndarray, counts: np.ndarray, quantile: float) -> float:
    cumulative = np.cumsum(counts)
    index = min(np.searchsorted(cumulative, quantile * cumulative[-1]), len(mids) - 1)
    return float(mids[index])


def cdf_at(mids: np.ndarray, counts: np.ndarray, bound_us: float) -> float:
    return float(counts[mids <= bound_us].sum() / counts.sum())


def extract_block(text: str, header_pattern: str) -> dict[str, float]:
    match = re.search(header_pattern + r"\n(?P<body>(?:  .*\n)+)", text)
    if not match:
        raise ValueError(f"statistics block not found: {header_pattern}")
    body = match.group("body")

    def number(label: str) -> float:
        item = re.search(rf"^  {re.escape(label)}\s+([0-9.eE+-]+)", body, re.MULTILINE)
        if not item:
            raise ValueError(f"field {label!r} not found in {header_pattern!r}")
        return float(item.group(1))

    return {
        "success": number("Successes:"),
        "failure": number("Failures:") if "Failures:" in body else np.nan,
        "throughput": number("Throughput:"),
        "loss_pct": number("Packet Loss:"),
    }


def load_stats(arm: str, k: int, seed: int) -> dict[str, float]:
    text = stem(arm, k, seed).with_suffix(".out").read_text()
    if arm == "edca":
        result = extract_block(text, r"AC_VO:")
        # The success block has no failure count; the later failure section does.
        failure = re.search(r"^AC_VO Failures:\s+(\d+)", text, re.MULTILINE)
        result["failure"] = float(failure.group(1)) if failure else 0.0
        result["n_target_sta"] = 30.0
    else:
        result = extract_block(text, rf"P-EDCA STAs \({k} STAs\):")
        result["n_target_sta"] = float(k)
    attempts = re.search(r"Global P-EDCA Attempt \(DS-CTS Sent\):\s+(\d+)", text)
    result["attempts"] = float(attempts.group(1)) if attempts else 0.0
    return result


def seed_metrics(arm: str, k: int, seed: int) -> dict[str, float]:
    mids, counts = load_hist(stem(arm, k, seed).with_suffix(".csv"))
    stats = load_stats(arm, k, seed)
    completion_survival = stats["success"] / (stats["success"] + stats["failure"])
    return {
        "p50_us": percentile(mids, counts, 0.50),
        "p95_us": percentile(mids, counts, 0.95),
        "p99_us": percentile(mids, counts, 0.99),
        "r10_pct": 100.0 * completion_survival * cdf_at(mids, counts, 10_000.0),
        "loss_pct": 100.0 * (1.0 - completion_survival),
        "throughput_per_sta": stats["throughput"] / stats["n_target_sta"],
        # FEM counters cover traffic from application start (0.5 s) through stop (8 s).
        "attempts_per_sta_s": stats["attempts"] / stats["n_target_sta"] / 7.5,
        "success": stats["success"],
        "failure": stats["failure"],
    }


rows: list[dict[str, float | str | int]] = []
seed_cache: dict[tuple[str, int], list[dict[str, float]]] = {}
for k in KS:
    for arm in ARMS:
        per_seed = [seed_metrics(arm, k, seed) for seed in SEEDS]
        seed_cache[(arm, k)] = per_seed
        mids, counts = pool_hist(arm, k)
        successes = sum(x["success"] for x in per_seed)
        failures = sum(x["failure"] for x in per_seed)
        survival = successes / (successes + failures)
        p99s = np.asarray([x["p99_us"] for x in per_seed]) / 1000.0
        r10s = np.asarray([x["r10_pct"] for x in per_seed])
        rows.append(
            {
                "nPedca": k,
                "arm": arm,
                "label": LABEL[arm],
                "parameters": PARAM[arm],
                "P50_ms": percentile(mids, counts, 0.50) / 1000.0,
                "P95_ms": percentile(mids, counts, 0.95) / 1000.0,
                "P99_ms": percentile(mids, counts, 0.99) / 1000.0,
                "P99_seed_sd_ms": float(p99s.std(ddof=1)),
                "loss_pct": 100.0 * (1.0 - survival),
                "R10_pct": 100.0 * survival * cdf_at(mids, counts, 10_000.0),
                "R10_seed_sd_pt": float(r10s.std(ddof=1)),
                "throughput_per_sta_Mbps": float(
                    np.mean([x["throughput_per_sta"] for x in per_seed])
                ),
                "attempts_per_sta_s": float(
                    np.mean([x["attempts_per_sta_s"] for x in per_seed])
                ),
            }
        )


for k in KS:
    by_arm = {str(row["arm"]): row for row in rows if row["nPedca"] == k}
    for row in by_arm.values():
        arm = str(row["arm"])
        row["P99_vs_default_pct"] = 100.0 * (
            float(row["P99_ms"]) / float(by_arm["default"]["P99_ms"]) - 1.0
        )
        row["R10_vs_default_pt"] = float(row["R10_pct"]) - float(
            by_arm["default"]["R10_pct"]
        )
        row["P99_vs_EDCA_pct"] = 100.0 * (
            float(row["P99_ms"]) / float(by_arm["edca"]["P99_ms"]) - 1.0
        )
        row["R10_vs_EDCA_pt"] = float(row["R10_pct"]) - float(by_arm["edca"]["R10_pct"])
        paired_p99 = np.asarray(
            [
                100.0 * (a["p99_us"] / b["p99_us"] - 1.0)
                for a, b in zip(seed_cache[(arm, k)], seed_cache[("default", k)])
            ]
        )
        paired_r10 = np.asarray(
            [
                a["r10_pct"] - b["r10_pct"]
                for a, b in zip(seed_cache[(arm, k)], seed_cache[("default", k)])
            ]
        )
        row["P99_paired_vs_default_mean_pct"] = float(paired_p99.mean())
        row["P99_paired_vs_default_sd_pct"] = float(paired_p99.std(ddof=1))
        row["R10_paired_vs_default_mean_pt"] = float(paired_r10.mean())
        row["R10_paired_vs_default_sd_pt"] = float(paired_r10.std(ddof=1))


fieldnames = list(rows[0])
with (HERE / "summary.csv").open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)


# Controller trajectory: QSRC occupancy plus the observed sticky k estimate.
trajectory_rows: list[dict[str, float | int | str]] = []
for k in KS:
    q_counts: Counter[int] = Counter()
    khat_final = []
    overhead_samples = []
    for seed in SEEDS:
        path = stem("adaptive", k, seed).with_name(stem("adaptive", k, seed).name + "_traj.csv")
        with path.open() as f:
            samples = list(csv.DictReader(f))
        q_counts.update(int(x["qsrc"]) for x in samples)
        khat_final.append(max(int(x["k_hat"]) for x in samples))
        overhead_samples.extend(0.185 * float(x["dscts_bursts"]) for x in samples)
    total = sum(q_counts.values())
    trajectory_rows.append(
        {
            "nPedca": k,
            "qsrc_mode": q_counts.most_common(1)[0][0],
            "q2_fraction": q_counts[2] / total,
            "q3_fraction": q_counts[3] / total,
            "q4_fraction": q_counts[4] / total,
            "q5_fraction": q_counts[5] / total,
            "max_khat_mean": float(np.mean(khat_final)),
            # 0.185 converts bursts/100 ms to estimated percent airtime.
            "stage1_airtime_est_mean_pct": float(np.mean(overhead_samples)),
        }
    )

with (HERE / "adaptive_trajectory_summary.csv").open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(trajectory_rows[0]))
    writer.writeheader()
    writer.writerows(trajectory_rows)


# Compact result figure: delivered tail and loss-aware deadline reliability.
fig, axes = plt.subplots(2, 3, figsize=(15, 7.5))
x = np.arange(len(ARMS))
for col, k in enumerate(KS):
    by_arm = {str(row["arm"]): row for row in rows if row["nPedca"] == k}
    p99 = [float(by_arm[a]["P99_ms"]) for a in ARMS]
    r10 = [float(by_arm[a]["R10_pct"]) for a in ARMS]
    colors = [COLOR[a] for a in ARMS]
    bars = axes[0, col].bar(x, p99, color=colors)
    for bar, value in zip(bars, p99):
        axes[0, col].text(bar.get_x() + bar.get_width() / 2, value, f"{value:.1f}",
                          ha="center", va="bottom", fontsize=8)
    axes[0, col].axhline(10, color="#9ca3af", ls=":", lw=1)
    axes[0, col].set_title(f"nPEDCA={k}: delivered P99")
    axes[0, col].set_ylabel("MAC delay (ms)")
    bars = axes[1, col].bar(x, r10, color=colors)
    for bar, value in zip(bars, r10):
        axes[1, col].text(bar.get_x() + bar.get_width() / 2, value, f"{value:.1f}",
                          ha="center", va="bottom", fontsize=8)
    axes[1, col].set_title(f"nPEDCA={k}: loss-aware R(10 ms)")
    axes[1, col].set_ylabel("on-time delivery (%)")
    axes[1, col].set_ylim(max(0, min(r10) - 4), 100.5)
    for row in range(2):
        axes[row, col].set_xticks(x)
        axes[row, col].set_xticklabels([LABEL[a] for a in ARMS], rotation=24, ha="right")
        axes[row, col].grid(axis="y", alpha=0.25)
fig.suptitle("On/Off saturation, 1 Mbps/STA, nSta=30, 3 paired seeds")
fig.tight_layout(rect=(0, 0, 1, 0.95))
fig.savefig(HERE / "delay_and_ontime_comparison.pdf")
fig.savefig(HERE / "delay_and_ontime_comparison.png", dpi=170)


report = [
    "# Burst-adaptive P-EDCA — On/Off saturation validation",
    "",
    "Three paired RngRun seeds, nSta=30, mean offered load 1 Mbps/STA, dual DS-CTS, "
    "1 s warmup and 7 s measurement window. Offline default is (0,2,1); offline best is "
    "the existing 10-run sweep's loss-aware R(10 ms) winner (0,4,3).",
    "",
    "| nPEDCA | arm | P50 ms | P95 ms | P99 ms | MAC loss % | R(10 ms) % | P99 vs default | R10 vs default |",
    "|---:|:---|---:|---:|---:|---:|---:|---:|---:|",
]
for row in rows:
    report.append(
        f"| {row['nPedca']} | {row['label']} {row['parameters']} | "
        f"{float(row['P50_ms']):.3f} | {float(row['P95_ms']):.3f} | "
        f"{float(row['P99_ms']):.3f} | {float(row['loss_pct']):.2f} | "
        f"{float(row['R10_pct']):.2f} | {float(row['P99_vs_default_pct']):+.1f}% | "
        f"{float(row['R10_vs_default_pt']):+.2f} pt |"
    )

report += [
    "",
    "## Adaptive trajectory",
    "",
    "| nPEDCA | modal QSRC | q2 | q3 | q4 | q5 | mean max k-hat | estimated Stage-1 airtime |",
    "|---:|---:|---:|---:|---:|---:|---:|---:|",
]
for row in trajectory_rows:
    report.append(
        f"| {row['nPedca']} | {row['qsrc_mode']} | {100*float(row['q2_fraction']):.1f}% | "
        f"{100*float(row['q3_fraction']):.1f}% | {100*float(row['q4_fraction']):.1f}% | "
        f"{100*float(row['q5_fraction']):.1f}% | {float(row['max_khat_mean']):.1f} | "
        f"{float(row['stage1_airtime_est_mean_pct']):.2f}% |"
    )

report += [
    "",
    "> Caveat: WifiTxStatsHelper loss counts completed MAC drops, while packets still queued "
    "at the simulation stop are not counted as losses. R(10 ms) therefore fixes delivered-only "
    "percentile bias but is not a full application-level deadline-delivery ratio.",
    "",
]
(HERE / "report.md").write_text("\n".join(report))

print("\n".join(report))
print(f"\nWrote {HERE / 'summary.csv'} and comparison figures")
