# ns-3 Implementation of Prioritized EDCA (P-EDCA) 🚀📶

[![ns-3 Version](https://img.shields.io/badge/ns--3-3.45-blue.svg)](https://www.nsnam.org/)
[![Standard](https://img.shields.io/badge/IEEE-802.11bn-green.svg)]()
[![Status](https://img.shields.io/badge/Status-Verification_%26_Optimization-orange.svg)]()

> **Note to Interviewers & Domain Experts:**
> This repository contains an implementation of **Prioritized EDCA (P-EDCA)**, a key channel access mechanism proposed in the IEEE 802.11be/bn (UHR) draft to reduce latency for high-priority traffic. Built on top of **ns-3.45**, this project details the core modifications to the Wi-Fi MAC layer necessary to resolve starvation issues for AC_VO (Voice/Low-Latency) traffic in extremely dense and congested WLAN environments.

---

## 📖 Background & Objective

In traditional 802.11 EDCA mechanisms, high-density networks lead to massive contention and elevated packet collision probabilities. For latency-sensitive traffic like AC_VO, consecutive collisions trigger exponential backoff window (CW) expansion, resulting in severe transmission delays and packet starvation.

**P-EDCA (Prioritized EDCA)** introduces a two-stage channel access mechanism:
1. **Stage 1 (Medium Reservation):** When a STA experiences consecutive transmission failures for AC_VO traffic (`QSRC ≥ 2`), it transitions to P-EDCA mode. After a shortened `DSAIFS` delay, the STA transmits a broadcast **DS-CTS (Defer Signal CTS)** control frame to silence the surrounding medium and reserve a dedicated 97µs Contention Window.
2. **Stage 2 (Prioritized Contention):** Upon successfully transmitting the DS-CTS, the STA enters the second stage. It **suspends contention for all other Access Categories (AC_VI, AC_BE, AC_BK)** and uses a highly compressed set of EDCA parameters (`CWmin=7, CWmax=7, AIFSN=2`) to exclusively contend and transmit AC_VO data.

**Project Objective:** To accurately model and implement the P-EDCA draft standard within the ns-3 Wi-Fi MAC architecture, and to build an automated simulation and data analysis pipeline to evaluate its performance under critical node densities.

---

## 🛠️ Core Architecture Modifications

This project entails deep modifications to the QoS MAC state machine and frame exchange control logic within the ns-3 framework:

| File Path | Modification Highlight |
| :--- | :--- |
| `src/wifi/model/qos-frame-exchange-manager.cc/h` | **[Core Logic]** Implemented the P-EDCA state machine including trigger conditions, DS-CTS construction and transmission (`StartTransmission()`), Stage 2 entry callbacks (`CTS TxEnd`), and algorithm overrides for CW/QSRC/PSRC upon CSMA/CA collisions. |
| `src/wifi/model/frame-exchange-manager.cc` | Modified the receiving logic (`UpdateNav()`) to ensure the standard-mandated P-EDCA MAC RA (`00:0F:AC:47:43:00`) is not filtered out, allowing idle STAs to correctly set their NAV to 97µs. |
| `src/wifi/model/qos-txop.cc` | Added the `SetPedcaBypassBackoff()` function to force P-EDCA nodes to immediately reset their backoff and enter Stage 2 contention after transmitting the DS-CTS. |
| `src/wifi/model/wifi-mac.cc` | Introduced the `PedcaSupported` node attribute, enabling the activation or deactivation of P-EDCA functions via simulation topology parameters. |

---

## 🔬 Technical Deep Dive: P-EDCA Timing & Operations

### 1. DS-CTS Frame Constraints (per 802.11bn)
To maintain backward compatibility with legacy CSMA/CA while maximizing coverage, strict transmission constraints are enforced for the DS-CTS:
- **Rate:** Non-HT OFDM 6 Mbps.
- **Receiver Address (RA):** Hardcoded to `00:0F:AC:47:43:00` (Standard designated).
- **Duration (NAV):** Hardcoded to 97µs (Account for SIFS + AIFSN + CWmax slot time).

### 2. Timing Accuracy Verification
The simulation rigorously validates the following timing behaviors:
* **Standard Scenario:** After the medium becomes idle, the STA transmits the DS-CTS after exactly `DSAIFS = SIFS (16µs) + AIFSN (2 × 9µs) = 34µs`.
* **Stage 2 Verification:** Trace log analysis confirms that the gap between the end of DS-CTS TX and the start of Data TX consistently falls within **[34µs, 97µs]**, ensuring it is never shorter than the standard AIFSN.
* **TIMING EXPIRED:** If the medium is occupied during Stage 2 and exceeds the 97µs window, the STA safely falls back to normal EDCA contention to prevent deadlocks.

---

## 📊 Current Status & Findings

The project is currently in the **Verification & Optimization Phase**. Key achievements include:

✅ **Core P-EDCA Logic Completed:** STAs accurately switch modes based on QSRC counters, send/receive DS-CTS, and execute Stage 2 contention with correct timing.  
✅ **Automated & Parallelized Data Pipeline:** Developed `scratch/delay_pdf/pdf_plot.py` using Python multiprocessing to execute concurrent ns-3 simulations, compute averages across multiple randomization seeds, and automatically generate performance plots (Delay Probability PDF/CDF, Throughput, Packet Loss Rate).  
✅ **Physical Layer Phenomenon Discovery (Half-duplex Limitation):** 
- Observed that ~25-30% of DS-CTS transmissions suffer from "same-slot collisions" under high contention.
- **Analysis:** NAV coverage across the BSS is not 100%. This is caused by the half-duplex nature of Wi-Fi—when a P-EDCA node transmits a DS-CTS, other nodes actively transmitting or receiving cannot decode the control frame. This is a correct emulation of physical constraints, not a software bug.

---

## 🚀 Next Steps (Future Work)

1. **Implement DSAIFS Randomization (`CWds`):**
   - Currently, `DSr=0` is hardcoded, causing multiple highly-delayed STAs to transmit DS-CTS on the exact same microsecond slot boundary.
   - We will introduce a `CWds` attribute (Default: 3 or 7) in `qos-frame-exchange-manager.cc` to randomize the DS-CTS transmission slot, significantly reducing DS-CTS collisions and improving NAV coverage.
2. **Fixed Scrambler Seed:** Enforce the standard requirement of overriding the DS-CTS transmission with `Seed=32`.
3. **Mixed Topology Simulations:** Evaluate the protection efficiency and fairness when P-EDCA-enabled STAs and legacy STAs coexist within the same BSS.

---

## 💻 Quick Start & Simulation Execution

### 1. Build Environment
```bash
cd /path/to/ns-3.45
./ns3 build
```

### 2. Run Single Verification Script
```bash
# Run topology with P-EDCA mechanism enabled (10 nodes, 5 seconds)
./ns3 run "scratch/pedca_verification_nsta.cc --nSta=10 --simTime=5.0 --dataRate=0.5Mbps"

# Baseline comparison: Traditional EDCA
./ns3 run "scratch/wifi_backoff80211n.cc --nSta=10 --simTime=5.0 --dataRate=0.5Mbps"
```

### 3. Parallelized Sweep & Plotting Pipeline
```bash
cd scratch/delay_pdf
# Run tests in parallel, then generate PDF/CDF distribution plots and stats
python3 pdf_plot.py 
```
*Upon completion, the script automatically generates `.csv` statistical data, performance summaries (`PEDCA_vs_EDCA_statistics_0.5Mbps.txt`), and `.pdf` plots for each `nSta` scenario.*

---

**Author:** [Nathan Lo/ WMNLab]  
**Date:** March 2026  
**Institution:** WMNLab  
*This implementation references IEEE P802.11bn™/D1.2 Clause 37.6.*
