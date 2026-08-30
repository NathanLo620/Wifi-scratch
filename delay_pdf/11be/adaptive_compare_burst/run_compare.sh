#!/usr/bin/env bash
set -euo pipefail

# Paired-seed On/Off saturation comparison for the burst-adaptive controller.
# Usage: bash run_compare.sh [parallel_jobs]

jobs="${1:-4}"
root="$(cd "$(dirname "$0")/../../../.." && pwd)"
out_dir="$root/scratch/delay_pdf/11be/adaptive_compare_burst"
binary="$root/build/scratch/ns3.45-pedca_nsta_onoff_11be-default"
mkdir -p "$out_dir"

run_one() {
    local arm="$1" k="$2" ratio="$3" seed="$4"
    local stem="$out_dir/${arm}_k${k}_s${seed}"
    local args=(
        --diagnosticLog=0 --nSta=30 --dataRate=1Mbps --simTime=8
        --RngRun="$seed" --pedcaRatio="$ratio" --clogFile=/dev/null
        --voicePdfOutput="${stem}_all.csv"
    )

    case "$arm" in
        edca)
            args+=(--adaptive=0 --voicePdfOutput="${stem}.csv")
            ;;
        default)
            args+=(--adaptive=0 --cwds=0 --qsrc=2 --psrc=1
                   --pedcaStaDelayOutput="${stem}.csv"
                   --legacyStaDelayOutput="${stem}_legacy.csv")
            ;;
        best)
            # Offline argmax of loss-aware R(10 ms) in the existing 10-run dual-DS sweep.
            args+=(--adaptive=0 --cwds=0 --qsrc=4 --psrc=3
                   --pedcaStaDelayOutput="${stem}.csv"
                   --legacyStaDelayOutput="${stem}_legacy.csv")
            ;;
        adaptive)
            # Start from the default triple; the AP begins controlling after the 1 s warmup.
            args+=(--adaptive=1 --policy=burstadaptive --cwds=0 --qsrc=2 --psrc=1
                   --pedcaStaDelayOutput="${stem}.csv"
                   --legacyStaDelayOutput="${stem}_legacy.csv"
                   --trajOutput="${stem}_traj.csv")
            ;;
        *)
            echo "unknown arm: $arm" >&2
            return 2
            ;;
    esac

    "$binary" "${args[@]}" > "${stem}.out" 2>&1
}
export -f run_one
export root out_dir binary

manifest="$out_dir/run_manifest.txt"
: > "$manifest"
for seed in 1 2 3; do
    # EDCA is independent of nPedca; analysis reuses this paired run for all three panels.
    printf 'edca 0 0 %s\n' "$seed" >> "$manifest"
    for spec in "5 0.1667" "15 0.5" "30 1.0"; do
        read -r k ratio <<< "$spec"
        for arm in default best adaptive; do
            printf '%s %s %s %s\n' "$arm" "$k" "$ratio" "$seed" >> "$manifest"
        done
    done
done

xargs -n4 -P "$jobs" bash -c 'run_one "$@"' _ < "$manifest"
