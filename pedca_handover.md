# P-EDCA Implementation & Analysis Handover Document
**Last Updated:** 2026-02-26
**Project:** ns-3 P-EDCA (Prioritized EDCA) Implementation
**Status:** Verification & Optimization Phase

---

## 1. Executive Summary
The P-EDCA mechanism (triggered by high `QSRC` retries) has been implemented in the `QosFrameExchangeManager`. Basic functionality is working: STAs correctly switch to P-EDCA, transmit DS-CTS, and attempt Stage 2 contention.

**Current Status:**
- ✅ Two-stage P-EDCA (DS-CTS + Stage 2 contention) fully implemented
- ✅ DS-CTS RA = `00:0F:AC:47:43:00` (per 802.11bn spec)
- ✅ DS-CTS Rate = 6 Mbps non-HT OFDM (per spec section 3.5)
- ✅ NAV correctly set at receiving STAs (verified via code trace + simulation)
- ✅ `pdf_plot.py` sweep tool: 5-run averaging, parallel execution, stats comparison
- ✅ DS-CTS NAV behavior verified with trace logging

**Current Bottleneck:**
~25-30% collision rate for DS-CTS frames when `CWds=0` (no DSAIFS randomization).
Multiple STAs trigger P-EDCA simultaneously, transmitting DS-CTS in the exact same slot.

**Immediate Next Step:** Implement **DSAIFS Randomization (CWds)**.

---

## 2. Codebase & Modified Files

### Core Implementation
*   **`src/wifi/model/qos-frame-exchange-manager.{cc,h}`**
    *   P-EDCA trigger logic in `StartTransmission()` (L245-576)
    *   DS-CTS construction & transmission (L366-400)
    *   Stage 2 entry via callback at CTS TxEnd (L460-502)
    *   Stage 2 timing validation: gap ≤ 97µs (L510-574)
    *   `TransmissionSucceeded()`: reset QSRC, PSRC (L959-983)
    *   `TransmissionFailed()`: QSRC++, Stage 2 collision CW expansion, backoff=0 override (L1026-1120)
    *   State variables: `m_pedcaPending`, `m_psrc`, `m_pedcaCtsTxEnd`, `m_pedcaStage2Active`
    *   Constants: `PEDCA_RETRY_THRESHOLD=2`, `PEDCA_CONSECUTIVE_ATTEMPT=1`

*   **`src/wifi/model/wifi-mac.{cc,h}`**
    *   `PedcaSupported` ns-3 attribute (bool, default=false)
    *   `SetPedcaSupported()` / `GetPedcaSupported()`

*   **`src/wifi/model/qos-txop.{cc,h}`**
    *   `SetPedcaBypassBackoff(bool bypass, uint8_t linkId)`: triggers new backoff for Stage 2

*   **`src/wifi/model/frame-exchange-manager.cc`**
    *   `UpdateNav()`: existing logic correctly handles DS-CTS (RA ≠ m_self → not skipped)
    *   `PostProcessFrame()`: called unconditionally in `Receive()` → NAV update always occurs for decoded frames
    *   **[TEMP]** Debug traces for DS-CTS NAV: `PsduRxError`, `Receive`, `UpdateNav` (to be removed)

### Verification Scripts
*   **`scratch/pedca_verification_nsta.cc`**: P-EDCA simulation, WifiTxStatsHelper output
*   **`scratch/wifi_backoff80211n.cc`**: Baseline EDCA simulation (same topology)
*   **`scratch/delay_pdf/pdf_plot.py`**: Parallel sweep with multi-run averaging

### Artifacts & Docs
*   **`scratch/Implementing P-EDCA.md`**: Detailed implementation spec (updated 2026-02-26)
*   **`scratch/P-EDCA mechanism.md`**: Original technical specification reference
*   **`scratch/run_sweep_pedca.py`**: Legacy sweep wrapper (superseded by `pdf_plot.py`)

---

## 3. Investigation & Validation Results

### 3.1 DS-CTS NAV Behavior (Verified 2026-02-24)

**Question:** When P-EDCA STAs send DS-CTS (RA=`00:0F:AC:47:43:00`), do other STAs correctly set NAV?

**Answer: YES, but only if their PHY is IDLE.**

Code trace verified:
```
PHY decode success
  → Receive() called
  → PostProcessFrame() called (OUTSIDE addr1 filter — always executed, L1302-1306)
  → UpdateNav() called
  → hdr.GetAddr1() == "00:0F:AC:47:43:00" ≠ m_self → NOT skipped
  → NAV updated with 97µs duration ✓
```

Simulation trace verified (`nSta=10, simTime=2.0s, dataRate=0.5Mbps`):

| DS-CTS TX time | #Senders | #NAV receivers | Max possible | Reason |
|:-:|:-:|:-:|:-:|:-:|
| t=121410µs | 1 | 2/10 | 10 | Low — many STAs in TX from prior collision |
| t=122516µs | 1 | 0/10 | 10 | All STAs busy |
| t=123043µs | **3 simultaneous** | 1/8 | 8 | DS-CTS collision + busy STAs |
| t=1310855µs | 1 | **10/10** | 10 | ✅ Full — all PHY IDLE after long idle |
| t=1311292µs | 1 | **10/10** | 10 | ✅ Full — all PHY IDLE |

**Key finding:** `PHY-ERROR = 0` — no DS-CTS decode failures observed. The partial NAV is caused by **receiving STAs being in TX/RX state** (half-duplex), not by PHY collision.

### 3.2 Why Partial NAV Occurs

**Root cause: Half-duplex + CSMA/CA same-slot collision.**

When multiple STAs' EDCA backoffs expire at the same slot boundary:
1. All STAs sense CCA IDLE at that instant (no one has started TX yet)
2. All start transmitting simultaneously (DS-CTS + VO data + VO data)
3. STAs in TX mode cannot receive (half-duplex)
4. Only IDLE STAs can receive the DS-CTS and set NAV

This is fundamental CSMA/CA behavior — CCA cannot prevent same-slot collisions.

### 3.3 Setup
*   **Scenario:** 10 STAs + 1 AP, Star topology, 1-5m random distance
*   **Traffic:** All 4 ACs (VO at 0.5Mbps per STA)
*   **PHY:** 802.11n 5GHz, HtMcs7, 20MHz, RTS/CTS enabled
*   **DS-CTS:** `OfdmRate6Mbps`, RA=`00:0F:AC:47:43:00`, Duration=97µs

### 3.4 Collision Statistics (nSta=10, 0.5Mbps)

| Statistic | Value | Interpretation |
|:---|:---|:---|
| **DS-CTS Collision Rate** | **~25-30%** | Multiple STAs transmit DS-CTS at the exact same timestamp |
| **Steady State NAV Success** | **~75%** | When no collision occurs, 75-100% of idle nodes update NAV |
| **Miss Reason: PHY TX/RX** | **Primary** | Receiving STA was transmitting or receiving another frame |
| **Miss Reason: PHY Decode Fail** | **0%** | No CRC/preamble failures observed for DS-CTS |
| **TIMING EXPIRED Rate** | **~30-50%** at high contention | Medium busy during Stage 2 → gap > 97µs → fallback |

---

## 4. Known Issues & Hypotheses

1.  **Synchronized DS-CTS Collisions (CWds=0):**
    *   *Observation:* Trace logs show `STAGE1` events occurring at identical timestamps for different STAs.
    *   *Cause:* Currently `DSr=0` (hardcoded). If two nodes fail standard EDCA and increment `QSRC` simultaneously, they both enter P-EDCA and transmit DS-CTS simultaneously.
    *   *Fix:* Implement `DSr` (Random Slot Selection) where `DSAIFS = SIFS + AIFSN + DSr`, with `DSr` uniform in `[0, CWds]`.

2.  **NAV "Partial" Failure:**
    *   When DS-CTS is sent, only IDLE-PHY STAs can receive and set NAV.
    *   STAs in TX/RX mode (from prior collision or ongoing transmission) do not receive DS-CTS.
    *   This is physically correct (half-duplex), not a bug.

3.  **Temporary Debug Traces:**
    *   `frame-exchange-manager.cc` contains `[TEMP TRACE]` markers for DS-CTS NAV debugging.
    *   `qos-frame-exchange-manager.cc` contains extensive `std::clog` traces (`[P-EDCA ...]`).
    *   These should be removed or gated behind NS_LOG for production.

---

## 5. Implementation Plan (Next Steps)

### Priority 1: Implement CWds (DSAIFS Randomization)
In `qos-frame-exchange-manager.cc`:
1.  Add a `CWds` attribute (default to 3 or 7, configurable).
2.  In `StartTransmission` at L331, replace `uint8_t dsr = 0;` with random draw from `[0, CWds]`.
3.  Schedule DS-CTS transmission after `DSAIFS = SIFS + (AIFSN + DSr) × SlotTime`.
4.  **Important:** Re-check channel idle before sending if delayed by DSr > 0.

### Priority 2: Re-verify with CWds > 0
1.  Run `pedca_verification_nsta.cc` with CWds > 0.
2.  Expectation:
    *   Same-timestamp DS-CTS events should drop near zero.
    *   NAV coverage should improve (more STAs IDLE when DS-CTS arrives).
    *   `TIMING EXPIRED` count should decrease.

### Priority 3: Clean Up Debug Traces
1.  Remove `[TEMP TRACE]` blocks in `frame-exchange-manager.cc` (PsduRxError, Receive, UpdateNav).
2.  Convert `std::clog` traces in `qos-frame-exchange-manager.cc` to `NS_LOG_DEBUG`.

### Priority 4: Full Performance Sweep
1.  Use `pdf_plot.py` (5-run averaging).
2.  Compare `nSta=2..50` with P-EDCA vs Standard EDCA.
3.  Metrics: **VO MAC Delay** (primary), Throughput, Packet Loss, Retransmission rate.

---

## 6. How to Run

### Build
```bash
cd /home/wmnlab/Desktop/ns-3.45
./ns3 build
```

### Single Simulation
```bash
# P-EDCA
./ns3 run "scratch/pedca_verification_nsta.cc --nSta=10 --simTime=5.0 --dataRate=0.5Mbps"

# EDCA baseline
./ns3 run "scratch/wifi_backoff80211n.cc --nSta=10 --simTime=5.0 --dataRate=0.5Mbps"
```

### Full Sweep (5 runs, parallel)
```bash
cd /home/wmnlab/Desktop/ns-3.45/scratch/delay_pdf
source ../.venv/bin/activate

# Run in background (survives terminal close)
nohup python3 -u pdf_plot.py > sweep_5runs.log 2>&1 &

# Monitor progress
tail -f sweep_5runs.log

# Kill all running simulations
pkill -f pdf_plot.py && pkill -f pedca_verification_nsta && pkill -f wifi_backoff80211n

# Plot only (skip simulations, use existing CSVs)
python3 pdf_plot.py --plot-only

# Override runs per scenario
python3 pdf_plot.py --runs 3
```

### Output Directory
```
scratch/delay_pdf/delay_result_0.5Mbps[2-50]_rts_on/
├── edca_vo_delay_pdf_nSta{N}_0.5Mbps.csv        # averaged histogram
├── pedca_vo_delay_pdf_nSta{N}_0.5Mbps.csv        # averaged histogram
├── vo_delay_probability_nSta{N}_0.5Mbps.pdf      # overlay plot
├── sim_log_nSta{N}_0.5Mbps.txt                   # log with averaged stats
└── PEDCA_vs_EDCA_statistics_0.5Mbps.txt           # side-by-side comparison
```

---

## 7. Key Code Locations Quick Reference

| What | File | Line(s) |
|------|------|---------|
| P-EDCA trigger | `qos-frame-exchange-manager.cc` | L248-260 |
| Deferral rules | `qos-frame-exchange-manager.cc` | L262-298 |
| DS-CTS construction | `qos-frame-exchange-manager.cc` | L366-400 |
| DS-CTS ForwardMpduDown | `qos-frame-exchange-manager.cc` | L400 |
| Stage 2 entry callback | `qos-frame-exchange-manager.cc` | L460-502 |
| Stage 2 timing check | `qos-frame-exchange-manager.cc` | L510-574 |
| TransmissionSucceeded reset | `qos-frame-exchange-manager.cc` | L959-984 |
| TransmissionFailed QSRC++/CW | `qos-frame-exchange-manager.cc` | L1026-1092 |
| Backoff=0 override | `qos-frame-exchange-manager.cc` | L1096-1120 |
| State variables | `qos-frame-exchange-manager.h` | L221-236 |
| PedcaSupported attribute | `wifi-mac.cc` | L83-89 |
| SetPedcaBypassBackoff | `qos-txop.cc` | L225 |
| UpdateNav (NAV logic) | `frame-exchange-manager.cc` | L1338-1407 |
| PostProcessFrame (always) | `frame-exchange-manager.cc` | L1318-1322 |
| [TEMP] DS-CTS NAV traces | `frame-exchange-manager.cc` | grep `TEMP TRACE` |
| DSr = 0 (hardcoded) | `qos-frame-exchange-manager.cc` | L331 |
