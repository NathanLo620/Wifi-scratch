# Technical Specification: Prioritized EDCA (P-EDCA) Operational Mechanism  
**Based on IEEE P82.11bn™/D1.1 (Sep 2025) — Clause 37.6 “Prioritized EDCA” and related definitions**  
**Updated with Comment Resolution items #4555, #7657, #12651**  
**Document Type:** Implementable mechanism spec (standard-setter / simulator-developer grade)  
**Version:** 1.1 (Mar 2026)

---

## 0. Scope

This document specifies the **normative operational behavior** of **Prioritized EDCA (P-EDCA)** for a **UHR non-AP STA** operating in a BSS where the AP has enabled P-EDCA.  
P-EDCA is an enhancement to EDCA that **reduces channel access delay for AC_VO traffic** by:
1) transmitting a **Defer Signal CTS (DS-CTS)** to protect a bounded contention interval via NAV, and  
2) immediately performing a constrained EDCA-like contention (**P-EDCA contention**) where **only EDCAF[AC_VO] may contend**, and where a TXOP must be initiated with **RTS**.

> **Non-goal (important):** DS-CTS does **not** reserve the medium for an entire payload TXOP by “Duration = TXOP length”. The DS-CTS Duration protects the **maximum P-EDCA contention duration** only.

---

## 1. Terminology and Key Variables

### 1.1 Entities
- **P-EDCA STA:** A STA with `dot11PEDCAOptionActivated = true`.
- **UHR AP:** AP that can enable P-EDCA for the BSS and provide P-EDCA parameters.

### 1.2 EDCA/EDCAF Terms (existing)
- **EDCAF[AC]:** EDCA function instance per Access Category.
- **TXOP:** Transmission Opportunity obtained via EDCA procedure.

### 1.3 P-EDCA Specific Frames and Timing
- **DS-CTS (Defer Signal CTS):** A CTS control frame transmitted by a P-EDCA STA to start P-EDCA contention.
- **DSAIFS[AC_VO]:** A slot-boundary transmission timing used to send DS-CTS:
  - `DSAIFS[AC_VO] = aSIFSTime + (AIFSN + DSr) × aSlotTime`
  - `AIFSN = 2`
  - `DSr` is uniformly random in `[0, CWds[AC_VO]]` **per DS-CTS transmission**.

### 1.4 P-EDCA Counters
- **PSRC[AC_VO] (P-EDCA STA retry counter):**
  - Initialized to 0.
  - Incremented by 1 **with every DS-CTS transmission**.
  - Set to 0 when `QSRC[AC_VO]` is set to 0.
- **QSRC[AC_VO]:** EDCAF retry-stage counter as defined in EDCA backoff procedure; used as a gate to allow entry to P-EDCA.
- **dot11ShortRetryLimit:** The maximum number of transmission attempts before a failure condition is indicated (see Section 3.1 for P-EDCA constraint).

### 1.5 Management / MAC Variables
- **dot11PEDCARetryThreshold:** Threshold to compare against `QSRC[AC_VO]` for allowing P-EDCA start.
- **dot11PEDCAConsecutiveAttempt:** Limit for consecutive DS-CTS transmissions (bounded by PSRC).

---

## 2. Capability Advertisement, Enablement, and Parameter Provisioning

### 2.1 STA Capability Signaling
- A STA with `dot11PEDCAOptionActivated = true` shall set **P-EDCA Support** in the **UHR MAC Capabilities** to 1; otherwise set it to 0.

### 2.2 Default State at (Re)Association
- When a non-AP STA that supports P-EDCA (re)associates, **P-EDCA mode is disabled by default** for that STA.

### 2.3 Enabling/Disabling P-EDCA Mode
- A UHR non-AP STA that intends to enable/disable P-EDCA shall follow the operating mode/parameter update procedure (UHR OMP update procedure).
- The associated AP shall accept the request and follow the same procedure to complete mode/parameter update.
- For a non-AP STA to enable P-EDCA, the AP must:
  - support P-EDCA, and
  - have P-EDCA enabled for the BSS.

### 2.4 AP Side BSS-Wide Enablement Indication
- An AP that has enabled P-EDCA operation shall set the **P-EDCA Enabled** field in the **UHR operation element** to 1.
- Parameter enable/disable/update notification shall follow the enhanced BSS parameter critical update procedure.

### 2.5 P-EDCA Operation Parameters (Mode Specific Parameters)
The **P-EDCA Operation Parameters field** contains:
- **P-EDCA ECWmin / ECWmax / AIFSN:** used **during P-EDCA contention** (not for DS-CTS slot timing).
  - Encoded exponentially:
    - `P-EDCA CWmin = 2^(P-EDCA ECWmin) - 1`
    - `P-EDCA CWmax = 2^(P-EDCA ECWmax) - 1`
  - Reserved constraints:
    - ECWmin / ECWmax values > 3 are reserved.
    - P-EDCA AIFSN values > 2 are reserved.
- **CW DS:** defines `CWds` used to randomize DS-CTS transmission slot:
  - value 0 means randomization not enabled
  - value 3 reserved
- **P-EDCA PSRC threshold:** maximum allowed consecutive DS-CTS transmissions
  - 0 reserved; values > 4 reserved
- **P-EDCA QSRC threshold:** required `QSRC[AC_VO]` level to start P-EDCA
  - 0 reserved

### 2.6 Default Parameter Set (Table 37-1)
If the most recent AP mode tuple for P-EDCA does **not** carry Mode Specific Parameters for P-EDCA, the default parameters below apply.

| AC    | P-EDCA CWmin | P-EDCA CWmax | P-EDCA AIFSN | P-EDCA contention duration | CWds | P-EDCA PSRC threshold | P-EDCA QSRC threshold |
|------|--------------:|--------------:|-------------:|---------------------------:|-----:|----------------------:|----------------------:|
| AC_VO| 7             | 7             | 2            | 97 µs                      | 0    | 1                     | 2                     |

**NOTE (normative rationale):**  
The NAV set by the DS-CTS Duration protects the medium for the **maximum P-EDCA contention duration**:
- `P-EDCA contention duration = aSIFSTime + (AIFSN + CWmax) × aSlotTime`
- For default values: `97 µs = 16 µs + (2 + 7) × 9 µs`  
**The value of P-EDCA contention duration is fixed and is not advertised by the AP.**

---

## 3. Conditions to Start a P-EDCA Contention

*(Updated per Comment Resolution #4555, #7657, #12651)*

A P-EDCA STA **may** start a P-EDCA contention if **all** of the following are satisfied:

1) **BSS + Link enablement:**
   - P-EDCA is enabled by the AP in the BSS, and
   - the P-EDCA non-AP STA has notified the AP of its intent to use P-EDCA on the link.
2) **Traffic condition:**
   - The P-EDCA STA has pending **AC_VO** buffered traffic.
3) **Gate by counters:**
   - `QSRC[AC_VO]` is **equal to or greater than** `dot11PEDCARetryThreshold`, and
   - `PSRC[AC_VO]` is **less than** `dot11PEDCAConsecutiveAttempt`

### 3.1 ShortRetryLimit Constraint (Normative)

> **NOTE (Comment Resolution #4555, #7657, #12651):** The additional condition required by a P-EDCA STA to start a P-EDCA contention is that `dot11ShortRetryLimit` is configured to be **higher than** `dot11PEDCARetryThreshold`.

**Rationale:** If `dot11ShortRetryLimit ≤ dot11PEDCARetryThreshold`, the EDCA retry procedure would indicate a failure condition (drop the MPDU) before QSRC can ever reach the P-EDCA threshold, making P-EDCA unreachable. Therefore, the standard requires:

```
dot11ShortRetryLimit > dot11PEDCARetryThreshold
```

For the default values:
- `dot11PEDCARetryThreshold = 2`
- `dot11ShortRetryLimit` must be configured to at least **3** (default is 7)

**Implementation note:** This constraint shall be validated at P-EDCA enablement time. If violated, P-EDCA shall not be activated, or the ShortRetryLimit shall be automatically raised to `dot11PEDCARetryThreshold + 1`.

---

## 4. Normative Procedure

### 4.0 Deferral Rules Prior to DS-CTS
Before attempting to transmit DS-CTS to start a P-EDCA contention, the STA shall follow applicable deferral rules including:
- EIFS deferral rule,
- CTSTimeout deferral rule,
- NAVTimeout deferral rule.

### 4.1 Step A — Transmit DS-CTS to Start P-EDCA Contention

#### 4.1.1 Compute DS-CTS Transmission Opportunity
- For each DS-CTS transmission attempt, select `DSr` uniformly in `[0, CWds[AC_VO]]`.
- The DS-CTS transmission shall occur at the **DSAIFS[AC_VO] slot boundary** if CS determines the medium idle:
  - `DSAIFS[AC_VO] = aSIFSTime + (2 + DSr) × aSlotTime`

#### 4.1.2 DS-CTS PHY/PPDU Requirements (hard constraints)
The DS-CTS frame shall be transmitted:
- in **non-HT PPDU** or **non-HT PPDU duplicate** format,
- using **6 Mb/s** data rate,
- with `SCRAMBLER_INITIAL_VALUE` fixed to `[0,0,0,0,0,1,0]` (seed value = 32).

#### 4.1.3 DS-CTS MAC Field Constraints (hard constraints)
- **RA field:** shall be set to the fixed MAC address:  
  `00:0F:AC:47:43:00`
- **Duration field:** shall be set to the value of **P-EDCA contention duration** in Table 37-1.

> **Operational meaning:** DS-CTS sets NAV at other STAs to protect only the bounded contention window for P-EDCA contention (not the full subsequent payload exchange).

### 4.2 Step B — Start P-EDCA Contention Immediately After DS-CTS

#### 4.2.1 Start Condition
- The P-EDCA contention shall start **immediately after the end** of the transmitted DS-CTS frame.

#### 4.2.2 Procedure Basis + Exceptions
The P-EDCA contention shall follow the random backoff procedure for obtaining an EDCA TXOP (EDCA TXOP obtaining procedure), **except that**:

1) **Single contender EDCAF:**
   - Only **EDCAF[AC_VO]** is allowed to contend during P-EDCA contention.
   - EDCAF[AC_VI], EDCAF[AC_BE], EDCAF[AC_BK] operations are **suspended**.

2) **Parameter initialization for EDCAF[AC_VO]:**
   - EDCAF[AC_VO] shall initialize:
     - `AIFSN := P-EDCA AIFSN`
     - `CWmin := P-EDCA CWmin`
     - `CWmax := P-EDCA CWmax`
   - `CW[AC_VO] := CWmin[AC_VO]`

3) **Backoff counter selection:**
   - EDCAF[AC_VO] shall set backoff counter to an integer drawn uniformly from:
     - `[0, CW[AC_VO]]`

### 4.3 Step C — Initiate TXOP (Mandatory RTS as Initial Frame)
A P-EDCA STA that initiates a TXOP during P-EDCA contention shall transmit an **RTS** frame as the **initial frame** in the TXOP.

> **Implication for implementers:** “Win contention → send RTS → wait for CTS → then continue TXOP exchanges per EDCA rules.”

---

## 5. Post-Contention Outcomes and State Transitions

### 5.1 Success Case: TXOP Obtained and MPDU(s) Successfully Delivered
If a P-EDCA STA successfully delivered one or more pending MPDUs in a TXOP obtained during P-EDCA contention (per EDCA success definition), then:

1) The STA shall not start P-EDCA contention again until the start conditions are satisfied (practically, success resets gating).
2) EDCAF[AC_VO] shall update:
   - `AIFSN, CWmin, CWmax := dot11EDCATable` (AP uses dot11QAPEDCATable)
3) EDCAF[AC_VI], EDCAF[AC_BE], EDCAF[AC_BK] operations are resumed.
4) `CW[AC_VO] := CWmin[AC_VO]`

### 5.2 Retry Case: Participated But No TXOP, or RTS Sent But No CTS Received
If the STA:
- participated in P-EDCA contention but did not initiate a TXOP, **or**
- initiated a TXOP but did not receive CTS in response to RTS,

then the STA may start another P-EDCA contention by sending DS-CTS again at a DSAIFS[AC_VO] slot boundary, when CS indicates medium idle, **for up to dot11PEDCAConsecutiveAttempt**.

> Remember: `PSRC[AC_VO]` increments on each DS-CTS transmission.

### 5.3 Exhaustion Case: PSRC Reaches dot11PEDCAConsecutiveAttempt
If `PSRC[AC_VO]` reaches `dot11PEDCAConsecutiveAttempt`:

1) The STA shall **not attempt** to start P-EDCA contention until:
   - `QSRC[AC_VO]` is reset, and
   - all start conditions are satisfied again.
2) EDCAF[AC_VO] shall update:
   - `AIFSN, CWmin, CWmax := dot11EDCATable` (AP uses dot11QAPEDCATable)
3) EDCAF[AC_VI], EDCAF[AC_BE], EDCAF[AC_BK] operations are resumed.
4) `CW[AC_VO]` shall be set to:
   - `CW[AC_VO] = min( CWmax[AC_VO], 2^(QSRC[AC]) × (CWmin[AC_VO] + 1) - 1 )`
   - (per EDCA backoff procedure reference)

---

## 6. Implementer-Oriented State Machine (Normative-Consistent)

### 6.1 High-Level States
- **S0: EDCA_NORMAL**
  - Standard EDCA across ACs.
- **S1: DS_CTS_ATTEMPT**
  - Evaluate start conditions; apply deferral rules; wait to DSAIFS slot boundary; send DS-CTS.
- **S2: PEDCA_CONTENTION**
  - Immediately after DS-CTS; suspend non-VO EDCAFs; run EDCA TXOP obtain procedure for AC_VO with P-EDCA parameters.
- **S3: TXOP_INIT_RTS**
  - If TXOP obtained, send RTS (mandatory), then proceed according to CTS reception and EDCA TXOP rules.
- **S4: RESTORE_EDCA**
  - On success or on PSRC exhaustion, restore EDCA parameters and resume other ACs.

### 6.2 Required Transitions
- S0 → S1: if start conditions satisfied.
- S1 → S2: after DS-CTS transmitted.
- S2 → S3: if TXOP obtained during P-EDCA contention.
- S2 → S1: if contention ended without TXOP (within allowed consecutive attempts).
- S3 → S1: if RTS sent but CTS not received (within allowed consecutive attempts).
- S3 → S4: if MPDU(s) successfully delivered in the TXOP.
- Any → S4: if PSRC reaches dot11PEDCAConsecutiveAttempt.

---

## 7. Reference Pseudocode (Simulator/NS-3 Grade)

```pseudo
# Preconditions updated continuously:
# - P-EDCA enabled by AP in BSS
# - STA has enabled P-EDCA on this link (OMP update completed)
# - pending AC_VO traffic exists
# - QSRC[VO] >= dot11PEDCARetryThreshold
# - PSRC[VO] < dot11PEDCAConsecutiveAttempt

function try_start_pedca():
  if not start_conditions_met(): return EDCA_NORMAL

  apply_deferral_rules(EIFS, CTSTimeout, NAVTimeout)

  # --- DS-CTS attempt ---
  DSr = uniform_int(0, CWds[VO])     # per DS-CTS transmission
  wait_until_DSAIFS_slot_boundary( aSIFSTime + (2 + DSr)*aSlotTime )
  if medium_idle_by_CS():
      transmit_DS_CTS_nonHT_6Mbps_scrambler_seed32(
          RA = 00:0F:AC:47:43:00,
          Duration = PEDCA_CONTENTION_DURATION   # from Table 37-1 default or updated set
      )
      PSRC[VO] += 1
  else:
      return EDCA_NORMAL

  # --- P-EDCA contention: starts immediately after DS-CTS ---
  suspend_EDCAFs_except(VO)
  set_EDCAF_params(VO, PEDCA_AIFSN, PEDCA_CWmin, PEDCA_CWmax)
  CW[VO] = CWmin[VO]
  backoff = uniform_int(0, CW[VO])

  result = obtain_TXOP_using_EDCA_rules_only_for_VO(backoff)
  if result == TXOP_OBTAINED:
      # mandatory RTS as initial frame
      send_RTS()
      if CTS_received():
          txop_success = deliver_one_or_more_MPDUs_per_EDCA_TXOP_rules()
          if txop_success:
              restore_EDCA_params_from_dot11EDCATable()
              resume_all_EDCAFs()
              CW[VO] = CWmin[VO]
              # (QSRC reset behavior is per EDCA success handling)
              return SUCCESS
          else:
              # EDCA failure handling updates QSRC/CW per EDCA rules
              pass
      # RTS but no CTS
      pass

  # either: no TXOP obtained OR RTS but no CTS OR TXOP failed
  if PSRC[VO] < dot11PEDCAConsecutiveAttempt:
      resume_all_EDCAFs()   # before next attempt, unless spec keeps suspended; conservative restore
      return try_start_pedca_again_when_medium_idle()
  else:
      # exhaustion behavior
      restore_EDCA_params_from_dot11EDCATable()
      resume_all_EDCAFs()
      CW[VO] = min(CWmax[VO], 2^(QSRC[AC])*(CWmin[VO]+1) - 1)
      return EDCA_NORMAL
