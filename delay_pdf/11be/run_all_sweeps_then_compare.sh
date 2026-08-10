#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Sequentially run every fix_nsta30_CwdsxQSRCxPSRC sweep (mono-DS then dual-DS)
#  and finish with the full cross-traffic-model comparison.
#
#  MUST be sequential: each sweep patches
#  src/wifi/model/qos-frame-exchange-manager.h (QSRC/PSRC) and runs `./ns3 build`,
#  so two sweeps in parallel would corrupt each other's binary.
#
#  Usage:
#    nohup bash run_all_sweeps_then_compare.sh > run_all.log 2>&1 &
#    BASE=/path/to/ns-3.45 bash run_all_sweeps_then_compare.sh      # other machine
#    EXTRA="--skip-existing" bash run_all_sweeps_then_compare.sh    # resume a crash
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

BASE="${BASE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
ROOT="$BASE/scratch/delay_pdf/11be"
PY="${PY:-python3}"
EXTRA="${EXTRA:-}"                 # e.g. --skip-existing / --runs 5 / --workers 20

MODELS=(
  fix_nsta30_CwdsxQSRCxPSRC_sweep
  fix_nsta30_CwdsxQSRCxPSRC_sweep_lightload
  fix_nsta30_CwdsxQSRCxPSRC_sweep_MMPP
  fix_nsta30_CwdsxQSRCxPSRC_sweep_onoff
  fix_nsta30_CwdsxQSRCxPSRC_sweep_poisson
)

ts() { date +%F_%T; }
FAILED=()

echo "##### START $(ts)  BASE=$BASE #####"

for m in "${MODELS[@]}"; do
  d="$ROOT/$m"
  if [[ ! -f "$d/sweep_pedca_count.py" ]]; then
    echo "!!! MISSING $d/sweep_pedca_count.py — skipped"
    FAILED+=("$m: script missing")
    continue
  fi
  for r in 1 2; do                 # 1 = mono-DS, 2 = dual-DS
    echo
    echo "##### $m dscts=$r $(ts) #####"
    "$PY" -u "$d/sweep_pedca_count.py" --dscts-repeat "$r" $EXTRA
    rc=$?
    if [[ $rc -ne 0 ]]; then
      echo "!!! FAILED $m dscts=$r (exit $rc) $(ts)"
      FAILED+=("$m dscts=$r (exit $rc)")
    else
      echo "##### DONE $m dscts=$r $(ts) #####"
    fi
  done
done

echo
echo "##### traffic_model_comparison $(ts) #####"
"$PY" -u "$ROOT/compare_traffic_models.py" --select-by p99
echo "##### traffic_model_comparison (ontime, 10ms deadline) $(ts) #####"
"$PY" -u "$ROOT/compare_traffic_models.py" --select-by ontime --deadline-ms 10

echo
if ((${#FAILED[@]})); then
  echo "##### ALL DONE WITH FAILURES $(ts) #####"
  printf '  - %s\n' "${FAILED[@]}"
  exit 1
fi
echo "##### ALL DONE $(ts) #####"
