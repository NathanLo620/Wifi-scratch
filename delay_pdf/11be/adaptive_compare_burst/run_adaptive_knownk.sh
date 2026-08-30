#!/usr/bin/env bash
set -euo pipefail

# Sensitivity arm: isolate policy quality from the LLI-only population estimator by giving
# the AP the number of associated P-EDCA stations and letting it converge during warmup.
jobs="${1:-4}"
root="$(cd "$(dirname "$0")/../../../.." && pwd)"
out_dir="$root/scratch/delay_pdf/11be/adaptive_compare_burst"
binary="$root/build/scratch/ns3.45-pedca_nsta_onoff_11be-default"

run_one() {
    local k="$1" ratio="$2" seed="$3"
    local stem="$out_dir/adaptive_knownk_k${k}_s${seed}"
    "$binary" \
        --adaptive=1 --policy=burstadaptive --kOverride="$k" --ctrlStart=0 \
        --cwds=0 --qsrc=2 --psrc=1 --diagnosticLog=0 --nSta=30 \
        --pedcaRatio="$ratio" --dataRate=1Mbps --simTime=8 --RngRun="$seed" \
        --clogFile=/dev/null --voicePdfOutput="${stem}_all.csv" \
        --pedcaStaDelayOutput="${stem}.csv" \
        --legacyStaDelayOutput="${stem}_legacy.csv" \
        --trajOutput="${stem}_traj.csv" > "${stem}.out" 2>&1
}
export -f run_one
export out_dir binary

manifest="$out_dir/adaptive_knownk_manifest.txt"
: > "$manifest"
for seed in 1 2 3; do
    printf '5 0.1667 %s\n15 0.5 %s\n30 1.0 %s\n' "$seed" "$seed" "$seed" >> "$manifest"
done
xargs -n3 -P "$jobs" bash -c 'run_one "$@"' _ < "$manifest"

