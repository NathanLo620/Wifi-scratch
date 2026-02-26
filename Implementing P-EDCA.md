# Implementing P-EDCA in ns-3.45 — Current State
**Last Updated:** 2026-02-26
**Status:** Implemented & Under Verification

---

## 1. Overview

P-EDCA (Prioritized EDCA) is a two-stage channel access mechanism specified in the 802.11be draft.
When a STA's VO transmissions repeatedly fail (QSRC ≥ 2), it switches to P-EDCA mode:
- **Stage 1:** Send a DS-CTS (Defer Signal CTS) to reserve a 97µs contention window
- **Stage 2:** Contend with reduced parameters (CW=7, AIFSN=2) during the reserved window

---

## 2. Modified Files Summary

| File | Purpose |
|------|---------|
| `src/wifi/model/qos-frame-exchange-manager.{h,cc}` | **Core P-EDCA logic**: trigger, DS-CTS TX, Stage 2 transition, parameter override, collision recovery |
| `src/wifi/model/wifi-mac.{h,cc}` | `PedcaSupported` attribute (bool, default=false) |
| `src/wifi/model/qos-txop.{h,cc}` | `SetPedcaBypassBackoff()` helper for Stage 2 backoff |
| `src/wifi/model/frame-exchange-manager.cc` | NAV update logic (UpdateNav), DS-CTS trace logging |
| `scratch/pedca_verification_nsta.cc` | Simulation script for P-EDCA verification |
| `scratch/wifi_backoff80211n.cc` | Baseline EDCA simulation for comparison |
| `scratch/delay_pdf/pdf_plot.py` | Parallel sweep: run 5x averaging, plot, stats comparison |

---

## 3. Detailed Implementation

### 3.1 State Variables (`qos-frame-exchange-manager.h`, L221-236)

```cpp
// P-EDCA state variables
bool m_pedcaPending{false};              // True after DS-CTS sent, waiting for Stage 2
uint8_t m_psrc{0};                       // P-EDCA STA Retry Counter (consecutive DS-CTS attempts)
// Note: m_qsrc is the existing QosFrameExchangeManager QSRC counter (reused)

// P-EDCA thresholds (per 802.11be draft spec)
static constexpr uint16_t PEDCA_RETRY_THRESHOLD = 2;       // dot11PEDCARetryThreshold
static constexpr uint8_t PEDCA_CONSECUTIVE_ATTEMPT = 1;    // dot11PEDCAConsecutiveAttempt

// P-EDCA timing tracking
Time m_pedcaCtsTxEnd{0};  // DS-CTS transmission end time for timing verification

// P-EDCA Stage 2 collision tracking
bool m_pedcaStage2Active{false};  // True when in Stage 2 contention
```

### 3.2 PedcaSupported Attribute (`wifi-mac.cc`, L83-89)

```cpp
.AddAttribute("PedcaSupported",
              "Whether P-EDCA is supported",
              BooleanValue(false),
              MakeBooleanAccessor(&WifiMac::SetPedcaSupported, &WifiMac::GetPedcaSupported),
              MakeBooleanChecker())
```
Enable in simulation:
```cpp
wifi.SetAttribute("PedcaSupported", BooleanValue(true));  // per-STA
```

### 3.3 P-EDCA Trigger Logic (`qos-frame-exchange-manager.cc`, L245-576)

Located in `QosFrameExchangeManager::StartTransmission()`:

```
Entry: StartTransmission(edca, txopDuration)
  ↓
Check: m_mac->GetPedcaSupported() && edca->GetAccessCategory() == AC_VO
  ↓
Trigger Check:
  - qsrcOk = (m_qsrc >= PEDCA_RETRY_THRESHOLD)    // QSRC ≥ 2
  - psrcOk  = (m_psrc < PEDCA_CONSECUTIVE_ATTEMPT)  // PSRC < 1
  ↓
Deferral Rules (L262-298):
  - waitingForResponse → defer
  - NAV active (!VirtualCsMediumIdle()) → defer
  - PHY busy (TX/RX/CCA/Switching) → defer
  - On deferral: NotifyChannelReleased + force backoff=0 for ASAP retry
  ↓
Stage 1 (L301-508):  qsrcOk && psrcOk && !m_pedcaPending
  → Construct DS-CTS frame
  → ForwardMpduDown (transmit)
  → PSRC++
  → Override EDCA params: CWmin=7, CWmax=7, AIFSN=2
  → Disable non-VO ACs during window
  → Schedule Stage 2 entry callback at CTS TxEnd
  → return false (no data yet)
  ↓
Stage 2 (L510-574):  m_pedcaPending == true
  → Check gap: (Now - m_pedcaCtsTxEnd) ≤ 97µs?
  → YES: stage2Valid = true, proceed with data TX
  → NO:  TIMING EXPIRED, fallback to normal EDCA
  → Always: restore VO default params (CWmin=3, CWmax=7, AIFSN=2)
```

### 3.4 DS-CTS Frame Construction (L366-400)

```cpp
WifiMacHeader ctsHeader;
ctsHeader.SetType(WIFI_MAC_CTL_CTS);
ctsHeader.SetDsNotFrom();
ctsHeader.SetDsNotTo();
ctsHeader.SetNoMoreFragments();
ctsHeader.SetNoRetry();
ctsHeader.SetAddr1(Mac48Address("00:0F:AC:47:43:00"));  // Fixed P-EDCA RA (per spec)
ctsHeader.SetDuration(MicroSeconds(97));                  // P-EDCA contention window

WifiTxVector ctsTxVector;
ctsTxVector.SetMode(WifiMode("OfdmRate6Mbps"));  // non-HT 6 Mbps (per spec)
ctsTxVector.SetPreambleType(WIFI_PREAMBLE_LONG);
ctsTxVector.SetTxPowerLevel(0);
ctsTxVector.SetChannelWidth(20);
```

**Key specs:**
- **RA = `00:0F:AC:47:43:00`** — fixed per 802.11be draft (NOT the STA's own address)
- **Duration = 97µs** — SIFS + AIFSN×Slot + CWmax×Slot ≈ 97µs
- **Rate = 6 Mbps** non-HT OFDM (per spec section 3.5)
- **Airtime ≈ 44µs** (24µs CTS payload + 20µs PHY header)

### 3.5 Stage 2 Entry Callback (L460-502)

After CTS TX ends, a callback waits for PHY to become IDLE:
```
Schedule at CTS_TxEnd:
  if PHY still in TX → retry every 1µs (up to 200 retries)
  else:
    → m_pedcaPending = true
    → m_pedcaCtsTxEnd = Simulator::Now()
    → SetPedcaBypassBackoff(true)  ← forces backoff generation
    → NotifyChannelReleased(edca) ← starts Stage 2 contention
```

### 3.6 TransmissionSucceeded (L959-983)

On **any VO TX success** (whether P-EDCA or normal EDCA):
```
QSRC = 0
PSRC = 0
m_pedcaStage2Active = false
m_pedcaPending = false
```

### 3.7 TransmissionFailed (L1026-1092)

Two paths:

**Path A: Stage 2 Collision** (`m_pedcaStage2Active == true`, L1031-1069):
```
CW expansion: CW = min(CWmax, 2^QSRC × (CWmin+1) - 1)
QSRC++
If PSRC >= PEDCA_CONSECUTIVE_ATTEMPT → PSRC = 0 (exhausted)
m_pedcaStage2Active = false
m_pedcaPending = false
```

**Path B: Normal VO Failure** (P-EDCA enabled, non-Stage-2, L1072-1085):
```
QSRC++
If m_pedcaPending → reset to false
```

**P-EDCA Priority Override** (L1096-1120):
When P-EDCA conditions are met (qsrcOk && psrcOk) at failure time:
```
Force backoff = 0 slots → immediate retry after AIFS
This gives P-EDCA VO higher priority than normal EDCA VO
```

### 3.8 NAV Handling for DS-CTS (`frame-exchange-manager.cc`)

**Reception chain:**
```
PHY decode success → Receive() → PostProcessFrame() → UpdateNav()
                                  (called OUTSIDE addr1 filter — always executed)
```

**In UpdateNav() (L1339-1345):**
```cpp
if (hdr.GetAddr1() == m_self)  // "00:0F:AC:47:43:00" != m_self → NOT skipped
    return;  // Only CTS-to-Self skips NAV update

// DS-CTS passes through → NAV is updated with 97µs duration ✓
```

**NAV is correctly set for DS-CTS** because:
1. `PostProcessFrame()` is called unconditionally in `Receive()` (L1302-1306)
2. DS-CTS RA `00:0F:AC:47:43:00` ≠ any STA's m_self → NAV update not skipped
3. Whether NAV is actually set depends on **PHY layer**: STA must be IDLE to receive

### 3.9 PedcaBypassBackoff (`qos-txop.cc`, L225)

```cpp
void QosTxop::SetPedcaBypassBackoff(bool bypass, uint8_t linkId)
{
    // When P-EDCA Stage 2 starts, force a new backoff with P-EDCA parameters
    // This ensures immediate contention after DS-CTS
    StartBackoffNow(GetBackoffSlots(linkId), linkId);
}
```

---

## 4. P-EDCA Timing

### Expected Timing (DSr = 0)

```
Last Busy End
  │
  ├── SIFS (16µs) ──┤
  │                  ├── AIFSN × Slot (2×9 = 18µs) ──┤
  │                  │                                 ├── DS-CTS TX (44µs) ──┤
  │                  │                                 │                       ├── Stage 2 Window (97µs) ──┤
  │                  │                                 │                       │                            │
  t₀                t₀+16µs                          t₀+34µs                 t₀+78µs                    t₀+175µs
                     ↑                                 ↑                       ↑
                     accessGrantStart                  DS-CTS TX start         CTS TxEnd → Stage 2 begins
```

### Stage 2 Contention Window

```
CTS TxEnd
  │
  ├── AIFS (34µs) ── min gap ──┤
  │                             ├── Backoff [0-7] × Slot (0-63µs) ──┤
  │                             │                                    │
  CTS TxEnd                    +34µs                                +97µs
  │                             ↑                                    ↑
  │                        Earliest data TX                     Latest data TX
  │                                                             (NAV expires)
```

**Verification criteria:**
- `Gap = DataTXStart - CTSTxEnd` must be ∈ [34µs, 97µs]
- Gap should NEVER be exactly 16µs (that would mean SIFS-only, which violates P-EDCA)

---

## 5. Simulation Setup

### `pedca_verification_nsta.cc` Parameters

| Parameter | Value |
|-----------|-------|
| PHY Standard | 802.11n 5GHz |
| Channel Width | 20 MHz |
| Guard Interval | 800ns |
| MCS | HtMcs7 (65 Mbps raw) |
| RTS/CTS | Enabled (threshold=0) |
| STA Layout | Random disc r ∈ [1, 5]m around AP at origin |
| Data Rate | 0.5 Mbps per STA (configurable) |
| Traffic Mix | All 4 ACs (BE, BK, VI, VO), equal share |
| Warm-up | 1.0s |
| P-EDCA | PedcaSupported=true for all STAs |

### `pdf_plot.py` Configuration

| Parameter | Value |
|-----------|-------|
| nSta sweep | 2, 4, 6, ..., 50 |
| Data rate | 0.5 Mbps |
| SimTime | 10s |
| Runs per scenario | 5 (averaged) |
| Workers | 20 parallel |
| RNG Seed | RngRun = 1..5 per run |

Output files (per nSta):
- `{scenario}_vo_delay_pdf_nSta{N}_{rate}.csv` — averaged histogram
- `vo_delay_probability_nSta{N}_{rate}.pdf` — overlay plot
- `sim_log_nSta{N}_{rate}.txt` — averaged statistics
- `PEDCA_vs_EDCA_statistics_{rate}.txt` — side-by-side comparison (all nSta)

---

## 6. Known Behaviors & Limitations

### 6.1 DS-CTS Collision (DSr = 0)
When multiple STAs trigger P-EDCA simultaneously (same slot boundary), all send DS-CTS at the same time.
- PHY collision: receiving STAs may fail to decode → NAV not set
- **Observed collision rate: ~25-30%** at high contention
- **Fix:** Implement CWds (DSr randomization), currently hardcoded to 0

### 6.2 Partial NAV Coverage
DS-CTS NAV is set only at STAs whose PHY is **IDLE** at reception time:
- STA in TX mode → half-duplex, cannot receive → NAV not set
- STA in RX mode → DS-CTS treated as interference → NAV not set
- **Verified**: 0 PHY-ERROR events even with 3 simultaneous DS-CTS (capture or TX-busy)
- **Low contention**: partial NAV (2-3 out of 10 STAs)
- **High idle**: full NAV (10/10 STAs)

### 6.3 TIMING EXPIRED
When medium is busy during Stage 2, the gap exceeds 97µs → P-EDCA falls back to EDCA.
This is correct spec behavior (NAV protection expired).

---

## 7. How to Build & Run

```bash
# Build
cd /home/wmnlab/Desktop/ns-3.45
./ns3 build

# Single P-EDCA simulation
./ns3 run "scratch/pedca_verification_nsta.cc --nSta=10 --simTime=5.0 --dataRate=0.5Mbps"

# Single EDCA baseline
./ns3 run "scratch/wifi_backoff80211n.cc --nSta=10 --simTime=5.0 --dataRate=0.5Mbps"

# Full sweep (5 runs × 25 nSta × 2 scenarios = 250 simulations)
cd scratch/delay_pdf
nohup python3 -u pdf_plot.py > sweep_5runs.log 2>&1 &
tail -f sweep_5runs.log  # monitor

# Plot only (skip simulations)
python3 pdf_plot.py --plot-only
```

---

## 8. TODO / Not Yet Implemented

- [ ] **CWds (DSAIFS Randomization):** `DSr` uniform in `[0, CWds]` to reduce DS-CTS collision
- [ ] **Scrambler Seed = 32:** Per spec, DS-CTS should use fixed scrambler seed (not implemented in ns-3 PHY)
- [ ] **Non-P-EDCA STA coexistence:** Current simulation has all STAs with P-EDCA enabled; testing with mixed STAs not done
- [ ] **Remove debug clog traces:** Temporary `[P-EDCA ...]` and `[DS-CTS ...]` traces in production code
- [ ] **Remove TEMP TRACE in frame-exchange-manager.cc:** PsduRxError, Receive, UpdateNav traces for DS-CTS NAV debugging