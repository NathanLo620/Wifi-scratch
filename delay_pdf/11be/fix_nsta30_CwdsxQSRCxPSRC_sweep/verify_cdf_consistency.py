#!/usr/bin/env python3
"""Verify that all-VO, P-EDCA, and legacy histogram CSVs are consistent."""

import argparse
import csv
from pathlib import Path


def load(path: Path, allow_empty=False):
    bins = {}
    probability_sum = 0.0
    if allow_empty and not path.exists():
        return bins, 0
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            key = (float(row["bin_start_us"]), float(row["bin_end_us"]))
            count = int(float(row.get("count", 0) or 0))
            probability = float(row["probability"])
            bins[key] = (count, probability)
            probability_sum += probability
    total_count = sum(count for count, _ in bins.values())
    if total_count <= 0:
        if allow_empty:
            return bins, 0
        raise AssertionError(f"{path}: histogram has no packet counts")
    if abs(probability_sum - 1.0) > 1e-5:
        raise AssertionError(f"{path}: probabilities sum to {probability_sum}")
    for key, (count, probability) in bins.items():
        expected = count / total_count
        # The simulator writes probabilities with about six significant
        # decimal digits, so allow the corresponding CSV round-off error.
        if abs(probability - expected) > 1e-6:
            raise AssertionError(
                f"{path}: bin {key} probability {probability} != {expected}"
            )
    return bins, total_count


def percentile(bins, total_count, target):
    cumulative = 0
    for (start, end), (count, _) in sorted(bins.items()):
        cumulative += count
        if cumulative / total_count >= target:
            return (start + end) / 2
    raise AssertionError("CDF did not reach the requested percentile")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", required=True, type=Path, dest="all_path")
    parser.add_argument("--pedca", required=True, type=Path)
    parser.add_argument("--legacy", required=True, type=Path)
    args = parser.parse_args()

    all_bins, all_count = load(args.all_path)
    pedca_bins, pedca_count = load(args.pedca, allow_empty=True)
    legacy_bins, legacy_count = load(args.legacy, allow_empty=True)

    if all_count != pedca_count + legacy_count:
        raise AssertionError(
            f"sample totals differ: all={all_count}, "
            f"pedca+legacy={pedca_count + legacy_count}"
        )

    keys = sorted(set(all_bins) | set(pedca_bins) | set(legacy_bins))
    cumulative_all = 0
    cumulative_parts = 0
    max_cdf_error = 0.0
    for key in keys:
        all_bin_count = all_bins.get(key, (0, 0.0))[0]
        part_bin_count = (
            pedca_bins.get(key, (0, 0.0))[0]
            + legacy_bins.get(key, (0, 0.0))[0]
        )
        if all_bin_count != part_bin_count:
            raise AssertionError(
                f"bin {key}: all={all_bin_count}, pedca+legacy={part_bin_count}"
            )
        cumulative_all += all_bin_count
        cumulative_parts += part_bin_count
        max_cdf_error = max(
            max_cdf_error,
            abs(cumulative_all / all_count - cumulative_parts / all_count),
        )

    if cumulative_all != all_count or cumulative_parts != all_count:
        raise AssertionError("CDF did not terminate at one")

    print("PASS: histogram counts and CDF mixture are consistent")
    print(
        f"samples: all={all_count}, pedca={pedca_count}, legacy={legacy_count}, "
        f"pedca_share={100.0 * pedca_count / all_count:.4f}%"
    )
    print(f"maximum CDF mixture error: {max_cdf_error:.3g}")
    for name, bins, count in (
        ("all", all_bins, all_count),
        ("pedca", pedca_bins, pedca_count),
        ("legacy", legacy_bins, legacy_count),
    ):
        if count == 0:
            print(f"{name}: no samples")
            continue
        values = [percentile(bins, count, p) for p in (0.5, 0.95, 0.99)]
        print(f"{name}: P50={values[0]:.1f}us P95={values[1]:.1f}us P99={values[2]:.1f}us")


if __name__ == "__main__":
    main()
