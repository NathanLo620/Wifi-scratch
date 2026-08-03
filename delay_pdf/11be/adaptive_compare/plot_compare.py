#!/usr/bin/env python3
"""CDF and percentile comparison for the four P-EDCA arms.

Arms: EDCA only / default P-EDCA (0,2,1) / best fixed P-EDCA (from the offline sweep) /
adaptive P-EDCA (loaddriven). On-Off traffic, 1 Mbps, nSta=30, 3 seeds pooled.

Delays come from the per-run histogram CSVs the scenario writes
(bin_start_us,bin_end_us,bin_mid_us,pdf_per_us,probability,count); counts are summed across
seeds before any percentile is taken, so every seed contributes in proportion to how many
packets it actually delivered.
"""
import csv
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SEEDS = [1, 2, 3]
KS = [5, 15]

# label -> (filename stem template, colour, linestyle)
ARMS = [
    ("EDCA only", "edca_k0_s{s}", "#6b7280", "--"),
    ("P-EDCA default (0,2,1)", "default_k{k}_s{s}", "#d97706", "-"),
    ("P-EDCA best fixed", "best_k{k}_s{s}", "#2563eb", "-"),
    ("P-EDCA adaptive", "adaptive_k{k}_s{s}", "#dc2626", "-"),
]
BEST_PARAM = {5: "(1,3,3)", 15: "(1,4,3)"}


def load_hist(stem, k):
    """Pool the per-seed histograms into one (bin_mid, count) pair of arrays."""
    mids, counts = [], []
    for s in SEEDS:
        path = os.path.join(HERE, stem.format(k=k, s=s) + ".csv")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for r in csv.DictReader(f):
                mids.append(float(r["bin_mid_us"]))
                counts.append(float(r["count"]))
    if not mids:
        return None, None
    mids = np.asarray(mids)
    counts = np.asarray(counts)
    order = np.argsort(mids)
    return mids[order], counts[order]


def cdf(mids, counts):
    c = np.cumsum(counts)
    return mids, c / c[-1]


def pct(mids, counts, q):
    """q-th percentile of the pooled histogram, in ms."""
    c = np.cumsum(counts)
    return float(mids[np.searchsorted(c, q * c[-1])]) / 1000.0


# --------------------------------- figure 1: CDF (top) and tail as CCDF (bottom)
fig, axes = plt.subplots(2, 2, figsize=(13, 9))
for col, k in enumerate(KS):
    ax, axt = axes[0][col], axes[1][col]
    for arm_i, (label, stem, colour, ls) in enumerate(ARMS):
        mids, counts = load_hist(stem, k)
        if mids is None:
            continue
        x, y = cdf(mids, counts)
        name = label + (f" {BEST_PARAM[k]}" if label == "P-EDCA best fixed" else "")
        ax.plot(x / 1000.0, y, color=colour, linestyle=ls, linewidth=1.9, label=name)
        # Same curve, zoomed to the top decile. On the full CDF above, everything from p90
        # to p100 is crammed into a tenth of the axis, which is exactly the region the arms
        # differ in. Here each arm's p99 crossing can simply be read off the x axis.
        axt.plot(x / 1000.0, y, color=colour, linestyle=ls, linewidth=1.9, label=name)
        p99 = pct(mids, counts, 0.99)
        axt.plot([p99], [0.99], marker="o", ms=5, color=colour, zorder=5)
        # stagger the labels: arms often land within a millisecond of each other
        axt.annotate(f"{p99:.1f} ms", xy=(p99, 0.99),
                     xytext=(6, -6 - 11 * arm_i), textcoords="offset points",
                     ha="left", fontsize=8, color=colour, fontweight="bold",
                     arrowprops=dict(arrowstyle="-", color=colour, lw=0.6,
                                     shrinkA=0, shrinkB=2))
    for a in (ax, axt):
        a.set_xscale("log")
        a.set_xlim(0.2, 60)
        a.grid(alpha=0.25, which="both")
    ax.axhline(0.99, color="#9ca3af", linewidth=0.7, linestyle=":")
    ax.axhline(0.95, color="#9ca3af", linewidth=0.7, linestyle=":")
    ax.text(0.02, 0.992, "p99", transform=ax.get_yaxis_transform(), fontsize=8, color="#6b7280")
    ax.text(0.02, 0.952, "p95", transform=ax.get_yaxis_transform(), fontsize=8, color="#6b7280")
    ax.set_ylim(0.5, 1.005)
    ax.set_xlabel("MAC delay (ms, log scale)")
    ax.set_ylabel("CDF")
    ax.set_title(f"nPedca = {k} / 30")
    ax.legend(loc="lower right", fontsize=8.5)

    axt.set_ylim(0.90, 1.002)
    axt.set_xlim(1, 30)
    for qv, nm in ((0.95, "p95"), (0.99, "p99")):
        axt.axhline(qv, color="#9ca3af", linewidth=0.8, linestyle=":")
        axt.text(0.015, qv + 0.0015, nm, transform=axt.get_yaxis_transform(),
                 fontsize=8.5, color="#6b7280")
    axt.set_xlabel("MAC delay (ms, log scale)")
    axt.set_ylabel("CDF")
    axt.set_title(f"nPedca = {k} / 30 — tail zoom (top 10% of packets)\n"
                  "dots mark where each arm crosses p99, labelled in ms",
                  fontsize=10)
    axt.legend(loc="lower right", fontsize=8.5)
fig.suptitle(
    "P-EDCA STA voice MAC delay — On-Off traffic, 1 Mbps/STA, nSta=30, 3 seeds pooled\n"
    "(EDCA-only arm has no P-EDCA STAs, so it shows all STAs)",
    fontsize=11,
)
fig.tight_layout(rect=[0, 0, 1, 0.95])
fig.savefig(os.path.join(HERE, "cdf_4arm_onoff_1Mbps.pdf"))
fig.savefig(os.path.join(HERE, "cdf_4arm_onoff_1Mbps.png"), dpi=150)

# ------------------------------------------------- figure 2: percentile comparison
fig2, axes2 = plt.subplots(1, 2, figsize=(13, 5.0))
QS = [(0.50, "P50"), (0.95, "P95"), (0.99, "P99")]
rows = []
for ax, k in zip(axes2, KS):
    width = 0.2
    xs = np.arange(len(QS))
    for i, (label, stem, colour, _ls) in enumerate(ARMS):
        mids, counts = load_hist(stem, k)
        if mids is None:
            continue
        vals = [pct(mids, counts, q) for q, _ in QS]
        rows.append((k, label, vals))
        bars = ax.bar(xs + (i - 1.5) * width, vals, width, label=label, color=colour, alpha=0.9)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v, f"{v:.1f}",
                    ha="center", va="bottom", fontsize=7.5)
    ax.set_xticks(xs)
    ax.set_xticklabels([n for _, n in QS])
    ax.set_ylabel("MAC delay (ms)")
    ax.set_title(f"nPedca = {k} / 30")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(fontsize=8.5)
fig2.suptitle(
    "P-EDCA STA voice MAC delay percentiles — On-Off traffic, 1 Mbps/STA, nSta=30, 3 seeds",
    fontsize=11,
)
fig2.tight_layout(rect=[0, 0, 1, 0.93])
fig2.savefig(os.path.join(HERE, "percentiles_4arm_onoff_1Mbps.pdf"))
fig2.savefig(os.path.join(HERE, "percentiles_4arm_onoff_1Mbps.png"), dpi=150)

# ------------------------------------------------------------------ text summary
out = os.path.join(HERE, "summary_4arm_onoff_1Mbps.csv")
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["nPedca", "arm", "P50_ms", "P95_ms", "P99_ms",
                "P99_vs_default_%", "P99_vs_EDCA_%"])
    for k in KS:
        d = {lab: v for kk, lab, v in rows if kk == k}
        base = d.get("P-EDCA default (0,2,1)", [None] * 3)[2]
        edca = d.get("EDCA only", [None] * 3)[2]
        for label, _stem, _c, _l in ARMS:
            if label not in d:
                continue
            v = d[label]
            w.writerow([k, label, f"{v[0]:.2f}", f"{v[1]:.2f}", f"{v[2]:.2f}",
                        f"{100*(v[2]-base)/base:+.1f}" if base else "",
                        f"{100*(v[2]-edca)/edca:+.1f}" if edca else ""])

print(f"{'k':>3} {'arm':26} {'P50':>7} {'P95':>7} {'P99':>7} {'vs default':>11} {'vs EDCA':>9}")
print("-" * 76)
for k in KS:
    d = {lab: v for kk, lab, v in rows if kk == k}
    base = d.get("P-EDCA default (0,2,1)", [None] * 3)[2]
    edca = d.get("EDCA only", [None] * 3)[2]
    for label, _s, _c, _l in ARMS:
        if label not in d:
            continue
        v = d[label]
        print(f"{k:>3} {label:26} {v[0]:>6.2f}ms {v[1]:>6.2f}ms {v[2]:>6.2f}ms "
              f"{100*(v[2]-base)/base:>+10.1f}% {100*(v[2]-edca)/edca:>+8.1f}%")
    print()
print("wrote cdf_4arm_onoff_1Mbps.{pdf,png}, percentiles_4arm_onoff_1Mbps.{pdf,png}, "
      "summary_4arm_onoff_1Mbps.csv")
