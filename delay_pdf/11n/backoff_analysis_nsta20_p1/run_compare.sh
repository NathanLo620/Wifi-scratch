#!/bin/bash
# Sweep RTS rate (OfdmRate6Mbps vs OfdmRate9Mbps), 10 seeds each.
# Captures one clog per (rate, seed) for downstream BO analysis.
set -e
cd /home/wmnlab/Desktop/ns-3.45
OUTDIR=scratch/delay_pdf/backoff_analysis_nsta20_p1
SCEN=scratch/pedca_verification_nsta.cc

run_with_rate() {
  local RATE=$1
  echo "=== Switching ControlMode to $RATE ==="
  sed -i "s|\"ControlMode\", StringValue(\"[A-Za-z0-9]*\"));|\"ControlMode\", StringValue(\"$RATE\"));|" "$SCEN"
  ./ns3 build pedca_verification_nsta 2>&1 | tail -2
  for seed in 1 2 3 4 5 6 7 8 9 10; do
    LOG="$OUTDIR/clog_${RATE}_seed${seed}.log"
    echo "  Seed=$seed → $LOG"
    ./ns3 run "pedca_verification_nsta --nSta=20 --pedcaRatio=0.05 --simTime=3 --dataRate=1Mbps --RngRun=$seed --clogFile=$LOG" \
      2>&1 | grep -E "STA0:|Avg P-EDCA Attempt" > "$OUTDIR/summary_${RATE}_seed${seed}.txt"
  done
}

run_with_rate OfdmRate6Mbps
run_with_rate OfdmRate9Mbps
echo "DONE — clog files in $OUTDIR/clog_*.log"
