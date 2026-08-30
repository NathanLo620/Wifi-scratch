#!/usr/bin/env bash
set -euo pipefail

# Final-policy delta run: only nPEDCA=30 changes when the large-population prior is q4.
root="$(cd "$(dirname "$0")/../../../.." && pwd)"
out_dir="$root/scratch/delay_pdf/11be/adaptive_compare_burst"
binary="$root/build/scratch/ns3.45-pedca_nsta_onoff_11be-default"

run_one() {
    local seed="$1"
    local stem="$out_dir/adaptive3_k30_s${seed}"
    "$binary" --adaptive=1 --policy=burstadaptive --cwds=0 --qsrc=2 --psrc=1 \
        --diagnosticLog=0 --nSta=30 --pedcaRatio=1.0 --dataRate=1Mbps --simTime=8 \
        --RngRun="$seed" --clogFile=/dev/null --voicePdfOutput="${stem}_all.csv" \
        --pedcaStaDelayOutput="${stem}.csv" --legacyStaDelayOutput="${stem}_legacy.csv" \
        --trajOutput="${stem}_traj.csv" > "${stem}.out" 2>&1
}
export -f run_one
export out_dir binary
printf '1\n2\n3\n' | xargs -n1 -P 3 bash -c 'run_one "$@"' _
