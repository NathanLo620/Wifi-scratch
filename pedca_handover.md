# P-EDCA Implementation & Analysis Handover Document
**Date:** 2026-01-29
**Project:** ns-3 P-EDCA (Prioritized EDCA) Implementation
**Status:** Verification & Optimization Phase

---

## 1. Executive Summary
The P-EDCA mechanism (triggered by high `QSRC` retries) has been implemented in the `QosFrameExchangeManager`. Basic functionality is working: STAs correctly switch to P-EDCA, transmit DS-CTS, and attempt Stage 2 contention.

**Current Bottleneck:**
Extensive analysis reveals a **~25-30% collision rate for DS-CTS frames**.
When P-EDCA triggers, multiple STAs often trigger it simultaneously (synchronized). Without the `CWds` (DSAIFS randomization) mechanism, they transmit DS-CTS in the exact same microsecond, causing physical layer decode failures at receivers. This leads to "partial NAV failure" and high `TIMING EXPIRED` rates.

**Immediate Next Step:** Implement **DSAIFS Randomization (CWds)**.

---

## 2. Codebase & Modified Files

### Core Implementation
*   **Path:** `src/wifi/model/qos-frame-exchange-manager.{cc,h}`
*   **Changes:**
    *   Implemented `TryStartPedcaTX()`: Checks `QSRC >= 2` and `PSRC < 1`.
    *   Implemented `SendDsCtsSequence()`: Handles the logic to send the DS-CTS control frame.
    *   State Management: Handles `m_pedcaPending`, timeouts, and Stage 2 fallback to EDCA.
    *   **Note:** Currently hardcoded `CWds = 0` (No randomization).

### Verification Scripts
*   **`scratch/pedca_verification_nsta.cc`**
    *   Main functional test. Sets up $N$ stations sending UDP AC_VO traffic.
    *   Uses `WifiTxStatsHelper` for detailed metrics (Throughput, Loss, Delay).
*   **`scratch/pedca_dscts_collision_analysis.cc`**
    *   Deep diagnostic tool created during debugging.
    *   Tracks PHY states (TX/RX/IDLE) of all nodes at the exact moment a DS-CTS is sent.
    *   Used to confirm "IDLE Miss" (Collision) vs "RX Busy" (Saturation).

### Artifacts & Docs
*   **`scratch/P-EDCA mechanism.md`**: Detailed technical specification used for implementation reference.
*   **`scratch/run_sweep_pedca.py`**: Python automation wrapper to run sweeps over $N$ stations.

---

## 3. Investigation & Validation Results

We performed a deep-dive analysis (Verification A & B) to understand why NAV coverage wasn't 100%.

### 3.1 Setup
*   **Scenario:** 10 STAs + 1 AP, Star topology, 5m distance.
*   **Traffic:** AC_VO (0.5Mbps - 5Mbps load).
*   **Control Mode:** `HtMcs0` (Validated).
*   **DS-CTS Rate:** `OfdmRate6Mbps` (Validated robust).

### 3.2 Findings (Based on Log Analysis)

| Statistic | Value | Interpretation |
| :--- | :--- | :--- |
| **DS-CTS Collision Rate** | **~25-30%** | Multiple STAs transmit DS-CTS at the exact same timestamp. |
| **Steady State Success** | **~75%** | When no collision occurs, 75% of nodes successfully decode and update NAV. |
| **Miss Reason: RX Busy** | **0% - 10%** | Receiver saturation is **NOT** the main problem in steady state. |
| **Miss Reason: IDLE** | **~20%** | Nodes were IDLE (ready to receive) but failed to decode. This confirms **Physical Collision**. |

**Conclusion:** The mechanism is functionally correct (NAV updates work), but performance is limited by the lack of `CWds`.

---

## 4. Known Issues & Hypotheses

1.  **Synchronized Collisions:**
    *   *Observation:* Trace logs show `STAGE1` events occurring at identical `Simulator::Now()` values for different STAs.
    *   *Cause:* Currently, P-EDCA simply waits for AIFS + 0 slots. If two nodes fail standard EDCA and increment `QSRC` simultaneously, they both enter P-EDCA and transmit DS-CTS simultaneously.
    *   *Fix:* Implement `DSr` (Random Slot Selection) where `DSAIFS = SIFS + AIFSN + DSr`, with `DSr` uniform in `[0, CWds]`.

2.  **NAV "Partial" Failure:**
    *   When DS-CTS collides, the PHY cannot decode the header.
    *   Result: `PhyRxMacHeaderEnd` does not fire -> `UpdateNav` is not called.
    *   Consequence: N-2 nodes do not set NAV, so they continue contending, potentially interrupting the P-EDCA Stage 2 sequences.

---

## 5. Implementation Plan (Next Steps)

### Priority 1: Implement CWds (DSAIFS Randomization)
In `qos-frame-exchange-manager.cc`:
1.  Add a `CWds` attribute (default to 3 or 7, configurable).
2.  In `StartTransmission` (or where P-EDCA is triggered), currently it likely calls `SendDsCtsSequence` immediately or after basic AIFS.
3.  **Action:** Calculate a random backoff slots count (`DSr`) uniformly from `[0, CWds]`.
4.  **Action:** Schedule `SendDsCtsSequence` to run after `AIFS + DSr * SlotTime`.
    *   *Careful consideration:* Ensure we check `IsBusy()` again before sending if we delayed.

### Priority 2: Re-verify Collision Rate
1.  Run `pedca_dscts_collision_analysis.cc` again with `CWds > 0`.
2.  Expectation:
    *   "Same-timestamp" events should drop near zero.
    *   "Success (Updated NAV)" should rise from ~75% to >90%.
    *   `TIMING EXPIRED` count in the main simulation should decrease.

### Priority 3: Final Performance Sweep
1.  Use `run_sweep_pedca.py`.
2.  Compare `nSta=10, 20, 30, 40` with P-EDCA (Fixed) vs Standard EDCA.
3.  Metrics to watch: **VO Latency** (primary goal) and **Throughput**.

---

## 6. How to Run Analysis

**To reproduce the collision findings:**
```bash
# Build (clean build recommended if switching branches)
./ns3 build scratch/pedca_dscts_collision_analysis

# Run analysis (Verification A - High Load)
./ns3 run 'pedca_dscts_collision_analysis --nSta=10 --simTime=2.0 --dataRate=5Mbps --bgTraffic=true'
```

**To run the main verification:**
```bash
# Run standard verification
./ns3 run 'pedca_verification_nsta --nSta=10 --simTime=5.0'
```
