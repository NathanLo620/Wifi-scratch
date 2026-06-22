# Implementing P-EDCA in ns-3.45 — Current State
**Last Updated:** 2026-05-09
**Status:** 2026-05-09 — Added per-DS-CTS attempt logging, partitioned delay metrics, AP-side DS-CTS NAV exemption (inert under HtMcs0 RTS), and backoff-vs-failure analysis pipeline. Earlier 2026-04-26 coexistence fixes still in place.

---

## 1. Overview

P-EDCA (Prioritized EDCA) is a two-stage channel access mechanism specified in the 802.11be/bn draft.
[cite_start]When a STA's VO transmissions repeatedly fail (QSRC ≥ 2) and the short retry limit allows[cite: 82], it switches to P-EDCA mode:
- [cite_start]**Stage 1:** Send a DS-CTS (Defer Signal CTS) initiated by EDCAF[AC_VO] [cite: 36, 40] [cite_start]to reserve a 77µs protected contention duration for the 5/6GHz band[cite: 141, 189].
- [cite_start]**Stage 2:** Contend with reduced parameters (CW=7, AIFSN=2) during the reserved window, while other EDCAFs are suspended with their states (Backoff, CWmin, CWmax, and QSRC) remaining unchanged[cite: 254].

---

## 2. Modified Files Summary

| File | Purpose |
|------|---------|
| `src/wifi/model/qos-frame-exchange-manager.{h,cc}` | **Core P-EDCA logic**: trigger, DS-CTS TX, Stage 2 transition, parameter override, collision recovery |
| `src/wifi/model/wifi-mac.{h,cc}` | `PedcaSupported` attribute (bool, default=false) |
| `src/wifi/model/qos-txop.{h,cc}` | `SetPedcaBypassBackoff()` helper for Stage 2 backoff |
| `src/wifi/model/frame-exchange-manager.cc` | NAV update logic (UpdateNav), DS-CTS trace logging |
| `src/wifi/model/phy-entity.cc` | 2026-04-26: DS-CTS fallback NAV and EIFS notification on additional preamble-drop reasons |
| `src/wifi/model/wifi-phy-listener.h` | 2026-04-26: added PHY-to-MAC NAV notification hook |
| `src/wifi/model/wifi-phy-state-helper.{h,cc}` | 2026-04-26: forwards fallback NAV notifications to PHY listeners |
| `src/wifi/model/channel-access-manager.cc` | 2026-04-26: consumes fallback NAV notifications and updates CAM NAV |
| `scratch/pedca_verification_nsta.cc` | Simulation script for P-EDCA verification |
| `scratch/pedca_verification_nsta_mod.cc` | **2026-05-09**: extended verification scratch; per-STA-type delay CSVs, extended stats block, per-DS-CTS backoff log, per-PHY TX event log |
| `scratch/wifi_backoff80211n.cc` | Baseline EDCA simulation for comparison |
| `scratch/delay_pdf/pdf_plot.py` | Parallel sweep: run 5x averaging, plot, stats comparison |
| `scratch/delay_pdf/fix_nsta20_mod/sweep_pedca_count.py` | **2026-05-09**: P-EDCA count sweep using `_mod` binary; produces per-STA-type delay PDF/CDF + 8 derived-metric plots |
| `scratch/delay_pdf/backoff_analysis_nsta20_p1/analyze_backoff_failure.py` | **2026-05-09**: backoff-slot vs failure-rate analysis (drawn slot 0–7 + slot7+pause categories) |
| `scratch/delay_pdf/backoff_analysis_nsta20_p1/analyze_collision_source.py` | **2026-05-09**: collision-source attribution from PHY TX events |
| `scratch/delay_pdf/backoff_analysis_nsta20_p1/analyze_pause_cause.py` | **2026-05-09**: which TX events occur during stage-2 backoff window |
| `scratch/delay_pdf/backoff_analysis_nsta20_p1/analyze_collision_v2.py` | **2026-05-09**: deferral analysis using actual PHY TX time |
| `scratch/delay_pdf/backoff_analysis_nsta20_p1/dump_collision_examples.py` | **2026-05-09**: human-readable timeline dump of collision events |

---

## 3. Detailed Implementation

### 3.1 State Variables (`qos-frame-exchange-manager.h`, L221-236)

```cpp
// P-EDCA state variables
bool m_pedcaPending{false};              // True after DS-CTS sent, waiting for Stage 2
uint8_t m_psrc{0};                       // P-EDCA STA Retry Counter (consecutive DS-CTS attempts)
// Note: m_qsrc is the existing QosFrameExchangeManager QSRC counter (reused)

// P-EDCA thresholds (per 802.11bn draft spec)
static constexpr uint16_t PEDCA_RETRY_THRESHOLD = 2;       // dot11PEDCARetryThreshold
static constexpr uint8_t PEDCA_CONSECUTIVE_ATTEMPT = 1;    // dot11PEDCAConsecutiveAttempt

// P-EDCA timing tracking
Time m_pedcaCtsTxEnd{0};  // DS-CTS transmission end time for timing verification

// P-EDCA Stage 2 collision tracking
bool m_pedcaStage2Active{false};  // True when in P-EDCA Stage 2 contention
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

```text
Entry: StartTransmission(edca, txopDuration)
  ↓
Check: m_mac->GetPedcaSupported() && edca->GetAccessCategory() == AC_VO
  ↓
Trigger Check:
  - qsrcOk = (m_qsrc >= PEDCA_RETRY_THRESHOLD)              // QSRC ≥ 2
  - psrcOk  = (m_psrc < PEDCA_CONSECUTIVE_ATTEMPT)          // PSRC < 1
  - [cite_start]retryLimitOk = (dot11ShortRetryLimit > PEDCA_RETRY_THRESHOLD) // Short retry limit check [cite: 82]
  ↓
Deferral Rules (L262-298):
  - waitingForResponse → defer
  - NAV active (!VirtualCsMediumIdle()) → defer
  - PHY busy (TX/RX/CCA/Switching) → defer
  - On deferral: NotifyChannelReleased + force backoff=0 for ASAP retry
  ↓
Stage 1 (L301-508):  qsrcOk && psrcOk && retryLimitOk && !m_pedcaPending
  [cite_start]→ Construct DS-CTS frame (Initiated by EDCAF[AC_VO]) [cite: 36, 40]
  → ForwardMpduDown (transmit)
  → PSRC++
  → Override EDCA params: CWmin=7, CWmax=7, AIFSN=2
  [cite_start]→ Suspend non-VO ACs (keep their Backoff, CW, QSRC unchanged) [cite: 254]
  → Schedule Stage 2 entry callback at CTS TxEnd
  → return false (no data yet)
  ↓
Stage 2 (L510-574):  m_pedcaPending == true
  [cite_start]→ Check gap: (Now - m_pedcaCtsTxEnd) ≤ 77µs? [cite: 141, 189]
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
ctsHeader.SetDuration(MicroSeconds(77));                  [cite_start]// P-EDCA 5/6GHz protected duration [cite: 141, 189]

WifiTxVector ctsTxVector;
ctsTxVector.SetMode(WifiMode("OfdmRate6Mbps"));  // non-HT 6 Mbps (per spec)
ctsTxVector.SetPreambleType(WIFI_PREAMBLE_LONG);
ctsTxVector.SetTxPowerLevel(0);
ctsTxVector.SetChannelWidth(20);
```

**Key specs:**
- **RA = `00:0F:AC:47:43:00`** — fixed per 802.11bn draft (NOT the STA's own address)
- [cite_start]**Duration = 77µs** — Reduced from 97µs to allow responder CTS transmission without NAV blocking (5/6GHz band)[cite: 141, 189].
- **Rate = 6 Mbps** non-HT OFDM (per spec section 3.5)
- **Airtime ≈ 44µs** (24µs CTS payload + 20µs PHY header)

### 3.5 Stage 2 Entry Callback (L460-502)

After CTS TX ends, a callback waits for PHY to become IDLE:
```text
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
```text
QSRC = 0
PSRC = 0
m_pedcaStage2Active = false
m_pedcaPending = false
// Resume suspended EDCAFs (VI, BE, BK)
```

### 3.7 TransmissionFailed (L1026-1092)

Two paths:

**Path A: Stage 2 Collision** (`m_pedcaStage2Active == true`, L1031-1069):
```text
CW expansion: CW = min(CWmax, 2^QSRC × (CWmin+1) - 1)
[cite_start]QSRC++ (Following baseline EDCA backoff procedure) [cite: 280, 281]
If PSRC >= PEDCA_CONSECUTIVE_ATTEMPT → PSRC = 0 (exhausted)
m_pedcaStage2Active = false
m_pedcaPending = false
```

**Path B: Normal VO Failure** (P-EDCA enabled, non-Stage-2, L1072-1085):
```text
QSRC++
If m_pedcaPending → reset to false
```

**P-EDCA Priority Override** (L1096-1120):
When P-EDCA conditions are met (qsrcOk && psrcOk && retryLimitOk) at failure time:
```text
Force backoff = 0 slots → immediate retry after AIFS
This gives P-EDCA VO higher priority than normal EDCA VO
```

### 3.8 NAV Handling for DS-CTS (`frame-exchange-manager.cc`)

**Reception chain:**
```text
PHY decode success → Receive() → PostProcessFrame() → UpdateNav()
                                  (called OUTSIDE addr1 filter — always executed)
```

**In UpdateNav() (L1339-1345):**
```cpp
if (hdr.GetAddr1() == m_self)  // "00:0F:AC:47:43:00" != m_self → NOT skipped
    return;  // Only CTS-to-Self skips NAV update

[cite_start]// DS-CTS passes through → NAV is updated with 77µs duration ✓ [cite: 141, 189]
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

### 3.10 2026-04-26 Coexistence Fixes: Stale Stage 2, EIFS, and DS-CTS Fallback NAV

This section documents the changes made on 2026-04-26 after mixed P-EDCA/legacy testing with
`nSta=20`, `pedcaRatio=0.05`, `dataRate=1Mbps`, and `simTime=10s`.

These changes supersede the older interpretation in §6.3 that every `gap > 77us` warning is only a
measurement artifact. The current implementation distinguishes between:
- `gap <= 77us`: inside the DS-CTS NAV protection window.
- `77us < gap <= 200us`: Stage 2 is late but still allowed to continue.
- `gap > 200us`: stale Stage 2; abort P-EDCA Stage 2 and release the channel access state.

#### 3.10.1 Stale Stage 2 Abort (`qos-frame-exchange-manager.cc`)

Earlier behavior left `m_pedcaPending=true` even when the Stage 2 RTS opportunity was delayed by
hundreds or thousands of microseconds. That allowed a stale P-EDCA Stage 2 RTS to be transmitted long
after the DS-CTS protection had expired.

Current behavior in the Stage 2 branch of `QosFrameExchangeManager::StartTransmission()`:
```text
gap = Now - m_pedcaCtsTxEnd

if gap <= 77us:
    Stage 2 timing OK; send Stage 2 RTS
else if gap <= 200us:
    DS-CTS NAV window expired, but still within Stage 2 deadline; send Stage 2 RTS
else:
    stale Stage 2; count Timing Expired, clear m_pedcaStage2Active, resume suspended ACs,
    NotifyChannelReleased(m_edca), clear m_edca, and return false
```

This prevents examples such as `gap=1223us`, `gap=2036us`, or larger stale pending attempts from
being sent as P-EDCA Stage 2 RTS.

#### 3.10.2 Additional EIFS Deferral on Preamble Drop (`phy-entity.cc`)

Before this fix, only `PREAMBLE_DETECT_FAILURE` consistently produced the deferral behavior needed
after a failed receive. In mixed DS-CTS/legacy RTS collisions, many receivers instead reported:
- `BUSY_DECODING_PREAMBLE`
- `PREAMBLE_DETECTION_PACKET_SWITCH`

Those receivers could resume EDCA access too early. The current code calls:
```cpp
m_state->NotifyPreambleDetectFailure(ppdu->GetTxVector());
```
for `BUSY_DECODING_PREAMBLE` and `PREAMBLE_DETECTION_PACKET_SWITCH` drops, so the
`ChannelAccessManager` applies EIFS-style deferral to the next access attempt.

This change is intentionally broader than DS-CTS only: it also affects legacy-only preamble failures.
That is why EDCA-only or mostly-legacy sweeps can show higher average MAC delay but lower failure
count.

#### 3.10.3 DS-CTS Fallback NAV When DS-CTS Cannot Be Decoded (`phy-entity.cc`)

Problem observed in logs:
- AP or legacy STAs can see/collide with a DS-CTS but fail to decode the MPDU.
- If the MPDU is not decoded, normal `FrameExchangeManager::UpdateNav()` does not run.
- Therefore AP/legacy STAs may not set the intended 77us DS-CTS NAV.

The current implementation detects a P-EDCA DS-CTS by header contents when the PPDU object is
available:
```cpp
header.IsCts() && header.GetAddr1() == Mac48Address("00:0F:AC:47:43:00")
```

For non-`TXING` preamble drops, it forwards a fallback NAV notification through:
```text
PhyEntity
  -> WifiPhyStateHelper::NotifyNavStart(duration)
  -> WifiPhyListener::NotifyNavStart(duration)
  -> ChannelAccessManager::NotifyNavStartNow(duration)
```

Important detail: the fallback NAV duration is aligned to the end of the DS-CTS frame:
```text
fallback duration = remaining DS-CTS RX time + DS-CTS Duration field
                  = remainingRx + 77us
```

The first implementation set only `77us` starting from the preamble-drop time, which made fallback
NAV expire about one DS-CTS airtime too early. That allowed legacy RTS to transmit even while the
proper decoded DS-CTS NAV would still have been active. This was corrected by adding the remaining
RX time before calling `NotifyNavStart`.

The transmitting STA is excluded (`reason == TXING`) because it cannot receive DS-CTS while it is
transmitting.

#### 3.10.4 AP Virtual Carrier Sense Uses CAM NAV (`frame-exchange-manager.cc`)

Fallback NAV is stored in the `ChannelAccessManager`. AP response logic previously checked only the
`FrameExchangeManager` MAC NAV via `m_navEnd`, so an AP could still respond even while the fallback
CAM NAV was active.

`FrameExchangeManager::VirtualCsMediumIdle()` now requires both NAV sources to be idle:
```cpp
const auto now = Simulator::Now();
return m_navEnd <= now &&
       (!m_channelAccessManager || m_channelAccessManager->GetNavEnd() <= now);
```

This makes AP CTS responses respect fallback DS-CTS NAV even when DS-CTS was not decoded through the
normal MAC path.

#### 3.10.5 Verification Snapshot After 2026-04-26 Fixes

Command:
```bash
./ns3 run "scratch/pedca_verification_nsta --nSta=20 --simTime=10 --dataRate=1Mbps --pedcaRatio=0.05 --RngRun=1 --clogFile=/tmp/pedca_nav_align_10s.log"
```

Single-run result:
```text
P-EDCA STA avg MAC delay: 3175.55 us
Legacy STA avg MAC delay: 7265.62 us
DS-CTS Sent: 182
Stage 2 Entered: 182
Stage 2 TX Started: 158
P-EDCA TX Success: 149
P-EDCA Success / DS-CTS: 81.8681%
P-EDCA Success / Stage2 TX Started: 94.3038%
P-EDCA Fail RTS Collision: 9
P-EDCA Fail Timing Expired: 24
```

10-run sweep comparison for `nSta=20`, `pedcaRatio=0.05`, `dataRate=1Mbps`:
```text
Before 2026-04-26 fixes:
  P-EDCA avg MAC delay: 5747.8 us
  Legacy avg MAC delay: 5124.9 us
  Total VO avg MAC delay: 5147.5 us
  Total failures: 2925.0
  Stage2 RTS collision: 193.4
  Timing expired: 259.3
  P-EDCA TX success: 173.9

After 2026-04-26 fixes:
  P-EDCA avg MAC delay: 4032.9 us
  Legacy avg MAC delay: 5891.0 us
  Total VO avg MAC delay: 5850.5 us
  Total failures: 1349.6
  Stage2 RTS collision: 23.1
  Timing expired: 33.5
  P-EDCA TX success: 237.2
```

Interpretation:
- P-EDCA Stage 2 reliability improved substantially.
- Overall failure count dropped.
- P-EDCA delay improved.
- Legacy delay increased because failed preamble receives now cause EIFS deferral and fallback
  DS-CTS NAV is applied when DS-CTS cannot be decoded.
- Total average MAC delay can increase because 19 legacy STAs dominate the success population in
  the `1 P-EDCA STA + 19 legacy STA` case.

#### 3.10.6 Remaining Review Questions

1. The current `200us` stale Stage 2 deadline is an engineering guard, not yet proven against the
   draft text. Review whether the deadline should be derived from a normative timer instead.
2. Stage 2 can still transmit after the 77us DS-CTS NAV window when the regenerated Stage 2 backoff
   is large, for example `gap=99us`, `108us`, or `111us`. In those cases, legacy STAs may legally
   resume contention after NAV expiry and collide with P-EDCA RTS.
3. The EIFS extension to `BUSY_DECODING_PREAMBLE` and `PREAMBLE_DETECTION_PACKET_SWITCH` improves
   correctness for failed receives but also affects legacy-only EDCA performance. Review whether it
   should be restricted to DS-CTS/RTS related failures or remain PHY-generic.
4. The fallback NAV relies on access to the PPDU/PSDU header even when MAC decode fails. This is
   useful for simulation instrumentation, but reviewers should confirm whether it is acceptable for
   the intended ns-3 abstraction level.

---

## 4. P-EDCA Timing

### Expected Timing (DSr = 0)

```text
Last Busy End
  │
  ├── SIFS (16µs) ──┤
  │                  ├── AIFSN × Slot (2×9 = 18µs) ──┤
  │                  │                                 ├── DS-CTS TX (44µs) ──┤
  │                  │                                 │                       ├── Stage 2 Window (77µs) ──┤
  │                  │                                 │                       │                            │
  t₀                t₀+16µs                          t₀+34µs                 t₀+78µs                    t₀+155µs
                     ↑                                 ↑                       ↑
                     accessGrantStart                  DS-CTS TX start         CTS TxEnd → Stage 2 begins
```

### Stage 2 Contention Window

```text
CTS TxEnd
  │
  ├── AIFS (34µs) ── min gap ──┤
  │                             ├── Backoff [0-7] × Slot (0-63µs) ──┤
  │                             │                                    │
  CTS TxEnd                    +34µs                                +77µs
  │                             ↑                                    ↑
  │                        Earliest data TX                     Latest data TX
  │                                                             (NAV expires)
```

**Verification criteria:**
- [cite_start]`Gap = DataTXStart - CTSTxEnd` must be ∈ [34µs, 77µs] [cite: 141, 189]
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

### 6.3 TIMING EXPIRED — Measurement Artifact (Not a Protocol Violation)

The "TIMING EXPIRED" warning (gap > 77µs) printed by Stage 2 is a **logging measurement artifact**, not a protocol error. The P-EDCA exchange still completes correctly.

**Root Cause 1: m_pedcaCtsTxEnd reference point is wrong.**

`m_pedcaCtsTxEnd` is assigned inside the PHY-idle polling callback, not at the actual CTS TX end time. The callback polls every 1µs for up to 200 iterations after the scheduled CTS airtime. By the time PHY becomes IDLE and the assignment executes, the actual CTS end has already passed — typically 15–20µs earlier.

Example from simulation log (nSta=2, t≈122ms):
```
t=122785µs: DS-CTS TX ends (actual CTS TxEnd)
t=122805µs: PHY idle callback fires → m_pedcaCtsTxEnd = 122805µs (20µs too late)
t=122884µs: Stage 2 RTS sent
  → Logged gap = 122884 - 122805 = 79µs  ← triggers TIMING EXPIRED warning
  → Actual gap = 122884 - 122785 = 99µs  ← AIFS(34µs) + 5 backoff slots(45µs) ✓
```

**Root Cause 2: Backoff logged as "0 slots" but is immediately overwritten.**

`SetPedcaBypassBackoff(true)` sets backoff=0 at the point of the Stage 2 callback. However, `NotifyChannelReleased()` is called immediately after and regenerates a fresh random backoff from `[0, CW=7]`. The Stage 2 log line prints the pre-regeneration value (0), while the STA actually contends with the regenerated backoff (e.g., 5 slots = 45µs), causing the logged gap to match AIFS + regenerated backoff — which can exceed 77µs.

**Protocol behavior is correct:**
- The DS-CTS NAV is correctly set at other STAs: `t=122805µs → NAV expires at t=122882µs (+77µs)`.
- The P-EDCA STA's RTS at `t=122884µs` arrives 2µs after NAV expiry — within the AP's AIFS guard time.
- RTS/CTS/data completes successfully. QSRC and PSRC reset to 0.

**Fix (applied 2026-04-01):** `m_pedcaCtsTxEnd` now assigned from `state->ctsTxEnd` (pre-computed as `Simulator::Now() + ctsAirtime` at DS-CTS scheduling time), not from `Simulator::Now()` inside the polling callback. Gap measurement is now correct.

**Post-fix gap breakdown (actual):**
```
gap(actual) = 99µs = polling_delay(20µs) + AIFS(34µs) + backoff_slots(45µs = 5 slots × 9µs)
```
The 20µs polling delay is still present because `NotifyChannelReleased` is not called until the PHY idle callback fires. The gap > 77µs is expected and correct per §4.2.3 (STA shall not abort Stage 2 backoff). The protocol succeeds because the AP's AIFS provides a further guard window after NAV expiry.

---

## 7. Verification Evidence (nSta=2, simTime=3s, dataRate=0.5Mbps)

Simulation command:
```bash
./ns3 run "pedca_verification_nsta --nSta=2 --simTime=3.0 --dataRate=0.5Mbps"
```

### Phase 1: Normal EDCA VO with QSRC increment (before P-EDCA trigger)
```
t=120006µs  [EDCA-VO] STA-02 wins medium, QSRC=0 → RTS sent
            → collision (no CTS) → QSRC=1
t=120620µs  [EDCA-VO] STA-02 wins medium, QSRC=1 → RTS sent
            → CTS timeout → QSRC=2
```
✅ QSRC correctly increments to 2 (= PEDCA_RETRY_THRESHOLD), gating P-EDCA.

### Phase 2: P-EDCA Stage 1 triggered — DS-CTS sent
```
t=122761µs  [P-EDCA Stage1] STA-02: QSRC=2 ≥ 2, PSRC=0 < 1 → trigger
            DS-CTS constructed: RA=00:0F:AC:47:43:00, Duration=77µs, 6Mbps OfdmRate
            PSRC → 1, non-VO ACs suspended, CWmin=7 CWmax=7 AIFSN=2 set
t=122785µs  DS-CTS TX ends (airtime = 24µs)
```
✅ Correct RA, duration, rate, PSRC increment, AC suspension, parameter override.

### Phase 3: DS-CTS NAV correctly set at other STAs
```
t=122805µs  [UpdateNav] STA-03 (AP): hdr.Duration=77µs → NAV set to t=122882µs
            (00:0F:AC:47:43:00 ≠ STA-03 m_self → NAV update not skipped) ✓
```
✅ NAV propagated via `UpdateNav()` in `PostProcessFrame()` (called unconditionally).  
✅ DS-CTS RA is NOT the receiving STA's own address → NAV update not suppressed.

### Phase 4: Stage 2 entry and backoff
```
t=122785µs  DS-CTS TX ends — m_pedcaCtsTxEnd = 122785µs (actual TX end, FIX applied)
t=122805µs  PHY idle callback fires; Stage 2 entered
            CW=7, pre-regen backoff=0 slots (before NotifyChannelReleased regenerates it)
            → NotifyChannelReleased() → actual backoff regenerated = 5 slots (45µs)
t=122884µs  Stage 2 RTS sent
            gap = 122884 - 122785 = 99µs (from actual CTS TX end)
            = 20µs (PHY idle polling delay) + 34µs (AIFS) + 45µs (5 slots × 9µs)
            → TIMING EXPIRED logged (99µs > 77µs NAV window)
            → Stage 2 continues per §4.2.3 (STA shall NOT abort backoff countdown) ✓
```
⚠️ Gap > 77µs is expected when backoff ≥ 3 slots, since AIFS(34) + 3×9=27 + 20µs polling = 81µs > 77µs.
The 77µs limit is the NAV protection window; the AP waits its own AIFS after NAV expiry before contending,
which provides additional coverage. See §6.3 for complete timing breakdown.

### Phase 5: RTS/CTS/Data completes — P-EDCA success
```
t=122964µs  CTS received from AP (80µs after RTS: SIFS(16µs) + CTS-TX(64µs))
t=123244µs  Data MPDU ACK received
t=123244µs  [P-EDCA SUCCESS] TransmissionSucceeded(): QSRC→0, PSRC→0
            m_pedcaStage2Active=false, m_pedcaPending=false
            Non-VO ACs resumed (VI, BE, BK state preserved unchanged)
```
✅ Full P-EDCA lifecycle completes correctly: DS-CTS → Stage2 → RTS → CTS → Data → ACK → reset.

---

## 8. How to Build & Run

```bash
# Build
cd ~/Desktop/ns-3.45
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

## 9. TODO / Not Yet Implemented

- [x] **Fix m_pedcaCtsTxEnd reference point (2026-04-01):** Now uses `state->ctsTxEnd` (pre-computed `Simulator::Now() + ctsAirtime`). Gap is now correctly measured from actual CTS TX end.
- [x] **Fix SetPedcaBypassBackoff / NotifyChannelReleased ordering (2026-04-01):** Stage 2 entry log now clarifies "pre-regen backoff" to avoid confusion with the actual backoff drawn by NotifyChannelReleased.
- [x] **Fix PSRC exhaustion blocking (2026-04-01):** Removed `m_psrc = 0` from Stage 2 failure exhaustion path. PSRC now stays ≥ dot11PEDCAConsecutiveAttempt after exhaustion, blocking P-EDCA re-entry until TransmissionSucceeded resets both QSRC and PSRC to 0.
- [x] **Add AIFSN nonzero check (2026-04-01):** Added D1.3 condition 4 (CIDs 7112/11411/11759): `AIFSN[AC_VO] > 0` must hold before triggering Stage 1. Verified AIFSN=2 in all trigger check logs.
- [x] **Non-P-EDCA STA coexistence (2026-05-09):** `pedca_verification_nsta_mod.cc` supports `--pedcaRatio` (fraction of STAs with P-EDCA enabled). Sweep + analysis pipeline added under `scratch/delay_pdf/fix_nsta20_mod/` and `backoff_analysis_nsta20_p1/`.
- [ ] **Implement CWds (DSr randomization):** Currently DSr=0 hardcoded; add per-DS-CTS random draw from `[0, CWds]` to reduce simultaneous DS-CTS collision rate (~25-30% at high contention).
- [ ] **Remove debug clog traces:** Temporary `[P-EDCA ...]` and `[DS-CTS ...]` traces in production code.
- [ ] **Remove TEMP TRACE in frame-exchange-manager.cc:** PsduRxError, Receive, UpdateNav traces for DS-CTS NAV debugging.

---

## 10. 2026-05-09 Additions: Per-Attempt Logging, Extended Stats, AP-Side NAV Plumbing

### 10.1 New per-DS-CTS attempt log (`qos-frame-exchange-manager.{h,cc}`)

Added a struct and vector to capture every Stage-2 attempt outcome for the P-EDCA STA:

```cpp
// in qos-frame-exchange-manager.h
struct PedcaAttemptRecord
{
    double dsCtsEndUs;     // when DS-CTS PHY TX ended
    double gapUs;          // effective gap from DS-CTS end to TX start (MAC-level)
    int    backoffSlots;   // reconstructed = round((gap-AIFS)/slot); -1 if unknown
    std::string outcome;   // SUCCESS / RTS_CTS_TIMEOUT / RTS_COLLISION / TIMING_EXPIRED / DEFERRAL
};
const std::vector<PedcaAttemptRecord>& GetPedcaAttempts() const { return m_pedcaAttempts; }

// private state
std::vector<PedcaAttemptRecord> m_pedcaAttempts;
double m_lastStage2GapUs{-1.0};
int    m_lastBackoffSlots{-1};
```

Hook points (all in `qos-frame-exchange-manager.cc`):
- Stage 2 timing check (~L545): captures `gapUs` and reconstructed `backoffSlots`, pushes `TIMING_EXPIRED` record if `gap > 200µs`.
- VO success path: pushes `SUCCESS` record when `m_pedcaStage2Active` and the data is acked.
- `TransmissionFailed(forceCurrentCw=true)`: pushes `RTS_CTS_TIMEOUT` (note: in the codebase comment this is labelled "CTS timeout"; it is actually the `forceCurrentCw=true` branch).
- `TransmissionFailed(forceCurrentCw=false)`: pushes `RTS_COLLISION` (no-CTS path triggered by `DoCtsTimeout`).
- DS-CTS deferral path (~L332): pushes `DEFERRAL` with `gap=0`.

### 10.2 Per-STA-type delay & extended stats in `pedca_verification_nsta_mod.cc`

Beyond the original `pedca_verification_nsta.cc`, this scratch:

1. Partitions WifiTxStatsHelper records into **P-EDCA STAs vs Legacy STAs** sets by node ID and computes:
   - per-type avg/median/P95/P99 queue / access / MAC delay
   - per-type throughput, packet loss, zero-retx fraction
2. Emits a machine-parseable `--- EXTENDED_STATS_BEGIN ---` … `--- EXTENDED_STATS_END ---` block on stdout containing keys like `PEDCA_STA_AVG_MAC_DELAY`, `LEGACY_STA_ZERO_RETX`, etc.
3. New CLI args:
   - `--pedcaStaDelayOutput=<csv>` — histogram CSV of MAC delays for P-EDCA STAs only
   - `--legacyStaDelayOutput=<csv>` — histogram CSV for legacy STAs only
   - `--backoffLogOutput=<csv>` — per-DS-CTS records dumped from `GetPedcaAttempts()` across all P-EDCA STAs
   - `--txEventLogOutput=<csv>` — every PHY TX event (`time_us, node_id, frame_type, size_bytes`) for collision-source attribution

`txEventLog` connects `Phy/PhyTxBegin` on every node and peeks the WifiMacHeader to label frames as `RTS / CTS / ACK / BACK / BAR / QOSDATA_TIDn / DATA / BEACON / MGT / CTL / OTHER`.

### 10.3 Sweep & plot pipeline (`scratch/delay_pdf/fix_nsta20_mod/sweep_pedca_count.py`)

Drives a fixed-`nSta=20` sweep over `nPedca = 0..20`, averaging across `N_RUNS` seeds. New plots beyond the original sweep:

| Plot | Source |
|---|---|
| `legacy_sta_delay_pdf_cdf_overlay_*.pdf` | per-`nPedca` legacy-STA delay PDF + CDF |
| `pedca_sta_delay_pdf_cdf_overlay_*.pdf` | per-`nPedca` P-EDCA-STA delay PDF + CDF |
| `per_sta_pedca_attempt_vs_pedca_count_*.pdf` | avg DS-CTS attempts per P-EDCA STA vs nPedca |
| `pedca_oneshot_success_vs_pedca_count_*.pdf` | `pedcaSuccess / dsCtsCount` vs nPedca |
| `edca_oneshot_success_vs_pedca_count_*.pdf` | zero-retx fraction (legacy STAs vs P-EDCA STAs) vs nPedca |
| `pedca_kickback_prob_vs_pedca_count_*.pdf` | total P-EDCA failures / DS-CTS sent |
| `pedca_rts_collision_prob_vs_pedca_count_*.pdf` | `failRtsCollision / Stage2 TX started` |
| `pedca_attempts_per_success_vs_pedca_count_*.pdf` | DS-CTS sent / P-EDCA success |
| `new_stats_combined_vs_pedca_count_*.pdf` | 6-subplot overview |

The pre-existing `combined_metrics_vs_pedca_count_*.pdf`, `vo_delay_pdf_cdf_overlay_*.pdf`, `pedca_success_share_*.pdf`, etc. remain.

### 10.4 Backoff vs failure analysis (`scratch/delay_pdf/backoff_analysis_nsta20_p1/`)

Five Python scripts on the `--backoffLogOutput` and `--txEventLogOutput` CSVs:

| Script | Output |
|---|---|
| `analyze_backoff_failure.py` | `01_gap_histogram.pdf`, `02_gap_by_outcome.pdf`, `03_failure_rate_by_backoff.pdf`, `04_outcome_pie.pdf`, `summary.txt`. Failure rate broken down by **drawn backoff slot 0..7** and a separate `slot7+pause` bucket (gap > 97µs but ≤ 200µs). |
| `analyze_collision_source.py` | `05_collision_offset_histogram.pdf`. For each `RTS_COLLISION`, finds the closest concurrent TX from another node (within ±20µs) and reports its frame type, role (AP / legacy), and time offset relative to the P-EDCA STA's RTS. |
| `analyze_pause_cause.py` | Categorises stage-2 backoff windows by drawn slot and prints which frames (AP ACK, legacy RTS, P-EDCA-STA RTS, AP CTS) appear during the window. Made the slot-7+pause root cause visible. |
| `analyze_collision_v2.py` | `06_actual_phy_gap_distribution.pdf`, `07_mac_gap_vs_phy_gap.pdf`. Uses actual PHY TX time (from `tx_events_*.csv`) to compute MAC→PHY deferral, not just the MAC-level `gap_us`. |
| `dump_collision_examples.py` | Human-readable timeline dump (`±200µs`) around N example RTS_COLLISION events. |

### 10.5 AP-side NAV plumbing (`frame-exchange-manager.{h,cc}`, `qos-frame-exchange-manager.cc`, `ht/ht-frame-exchange-manager.cc`)

To prepare for "AP must honour DS-CTS NAV" experiments, these AP-side hooks were added. **They are deliberately benign with the current HtMcs0 control mode** (the entire RTS-CTS-DATA-ACK exchange runs past the 77µs NAV window, so the conditions never trigger). They will activate if RTS is shortened (e.g. `OfdmRate24Mbps` → 28µs RTS).

#### 10.5.1 New member: tag NAV as DS-CTS-derived

```cpp
// frame-exchange-manager.h, alongside m_navEnd
Time m_navEndFromDsCts{0};   // NAV expiry time *if* set by a DS-CTS frame
```

#### 10.5.2 Tag the NAV in `UpdateNav`

```cpp
// frame-exchange-manager.cc, inside the navEnd > m_navEnd branch
if (hdr.IsCts() && hdr.GetAddr1() == Mac48Address("00:0F:AC:47:43:00"))
{
    m_navEndFromDsCts = navEnd;   // remember this NAV came from a DS-CTS
    std::clog << "[DS-CTS NAV-SET] STA=" << m_self << " NAV set to "
              << m_navEnd.GetMicroSeconds() << "us …" << std::endl;
}
```

#### 10.5.3 Suppress AP ACK / BACK during DS-CTS NAV

```cpp
// FrameExchangeManager::SendNormalAck (early return)
if (m_navEndFromDsCts > Simulator::Now()) {
    std::clog << "[DS-CTS ACK-SUPPRESS] STA=" << m_self << " skipping ACK to "
              << hdr.GetAddr2() << " …" << std::endl;
    return;
}

// HtFrameExchangeManager::SendBlockAck (early return)
if (m_navEndFromDsCts > Simulator::Now()) {
    std::clog << "[DS-CTS BACK-SUPPRESS] STA=" << m_self << " skipping BlockAck …"
              << std::endl;
    return;
}
```

Goal: stop the AP from finishing a SIFS-bounded reply for legacy DATA inside the 77µs window.

#### 10.5.4 Allow AP to reply CTS to a Stage-2 RTS even while DS-CTS NAV is active

Standard 802.11 says "if NAV is busy, do not respond with CTS". That blocks the AP from CTS-ing the P-EDCA winner's Stage-2 RTS when the AP itself has just set NAV from DS-CTS. Patched both code paths:

```cpp
// FrameExchangeManager::Receive RTS branch (frame-exchange-manager.cc ~L1521)
const bool dsCtsNavExempt = (m_navEndFromDsCts > Simulator::Now());
if (VirtualCsMediumIdle() || dsCtsNavExempt) {
    // schedule CTS reply
}

// QosFrameExchangeManager::ReceiveMpdu RTS branch (qos-frame-exchange-manager.cc ~L1465)
const bool dsCtsNavExempt = (m_navEndFromDsCts > Simulator::Now());
if (hdr.GetAddr2() == m_txopHolder || VirtualCsMediumIdle() || dsCtsNavExempt) {
    // schedule CTS reply
}
```

#### 10.5.5 Why these are no-ops with HtMcs0 RTS

| Slot | gap (µs) | RTS end | CTS start | DS-CTS NAV end |
|---:|---:|---:|---:|---:|
| 0 | 34 | 98 | 114 | 77 |
| 7 | 97 | 161 | 177 | 77 |

By the time AP wants to reply CTS (≥ 98µs after DS-CTS end), the DS-CTS NAV (77µs) has already expired — `VirtualCsMediumIdle()` is already true, no exemption needed. Same for any pending ACK: `Now > 77µs` ⇒ suppression condition false.

Verified empirically (10s sim, `nSta=20`, `pedcaRatio=0.05`, `RngRun=1`, `HtMcs0`):

```text
DS-CTS NAV-SET            : 1161   (NAV set on every receiver including AP)
[DS-CTS ACK-SUPPRESS]     :    0
[DS-CTS BACK-SUPPRESS]    :    0
P-EDCA RTS-EXEMPT applied :    0
```

Outcome distribution unchanged from pre-2026-05-09:

```text
SUCCESS         : 2371 (80.76 %)
RTS_COLLISION   :  231 ( 7.87 %)
TIMING_EXPIRED  :  334 (11.38 %)
TOTAL           : 2936
```

Trade-off observed when temporarily switching ControlMode to `OfdmRate24Mbps` (28µs RTS, NOT in current default): slot-7+pause cases collapsed almost to zero (288 → 8) but slot-0/1 failures shot up because the *entire* RTS-CTS-DATA-ACK no longer fits in 77µs, so legacy STAs collided with the post-NAV portion. ControlMode reverted to `HtMcs0` for production.

### 10.6 Clarification on TIMING_EXPIRED (refines §6.3)

`TIMING_EXPIRED` is a hard abort, not "succeeded but past the 200µs deadline":

```text
gap ≤ 77us         → ✓ TIMING OK             (Stage 2 RTS is sent)
77us < gap ≤ 200us → ⚠ NAV WINDOW EXPIRED    (Stage 2 RTS is still sent, unprotected)
gap > 200us        → ✗ STALE STAGE 2         (NO RTS sent; resume ACs, return false)
```

Implications for the metric "11.38 % TIMING_EXPIRED":
- The DS-CTS for that attempt is wasted (Stage 1 transmitted but Stage 2 aborted).
- The underlying VO MPDU is **not** lost — it stays in the WifiMacQueue and is retried via regular EDCA contention. That is why `Global EDCA Tx Success` (~1006) and `Global P-EDCA Tx Success` (~138) coexist for `pedcaRatio=0.05`.
- "Failure of the P-EDCA mechanism" = `(RTS_CTS_TIMEOUT + RTS_COLLISION + TIMING_EXPIRED) / DS-CTS sent`.
- "Failure of the underlying packet" = WifiTxStatsHelper `failures` counter, much smaller.

### 10.7 Output artefacts (per run)

A typical `--backoffLogOutput` row:
```csv
sta_id,ds_cts_end_us,gap_us,backoff_slots,outcome
0,980795,108,8,RTS_COLLISION
0,1.03706e+06,90,6,SUCCESS
…
```

A typical `--txEventLogOutput` row:
```csv
time_us,node_id,frame_type,size_bytes
1054640,8,RTS,20
1054640,11,RTS,20
1054640,0,RTS,20
1054800,20,CTS,14
…
```

The `tx_events` log is large (≈ 110k events for a 10s 20-STA run) but enables collision-source attribution and pause-cause analysis without further simulation re-runs.
