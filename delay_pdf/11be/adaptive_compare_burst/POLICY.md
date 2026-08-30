# Adaptive P-EDCA policy design

## Control objective

The primary objective is loss-aware deadline delivery, `R(10 ms)`, rather than the P99 of
delivered packets alone.  The latter can improve merely because late packets were dropped or
were still queued when the simulation stopped.

QSRC is treated as a selector of the stations allowed to invoke P-EDCA, not as a monotonic
"more/less conservative" quality knob.  In particular, QSRC=1 is avoided: eligibility is
conditioned on the immediately preceding EDCA collision, so the stations that collided
together are preferentially admitted to Stage 1 together.

## Implemented bursty-safe branch (`burstadaptive`)

1. The AP estimates the sticky number `k_hat` of proven P-EDCA stations from LLI feedback.
2. A population prior supplies the nominal QSRC:
   - `k_hat=0`: QSRC 2 while capability evidence is absent;
   - `1..8`: QSRC 3;
   - `>=9`: QSRC 4.
3. Stage-1 airtime, LLI urgency and DS-CTS burst loss are filtered with EWMA alpha 0.25.
4. QSRC rises by one only after two consecutive overload periods.  Overload means Stage-1
   airtime above 12%, or severe DS-CTS burst loss above 50% with at least 20 observed bursts.
5. QSRC 5 is an overload state, not a population prior. It returns to the QSRC 3/4 prior after
   five consecutive periods with high urgency, Stage-1 airtime below 9%, and no severe DS-CTS
   loss.
6. QSRC is clamped to `[population lower bound, 5]`; QSRC 0 and 1 are not explored in this branch.
7. PSRC is 3.  CWds is 1 because the existing sweep makes CWds a weak dimension and one slot
   of randomisation is cheap protection against Stage-1 ties.
8. The control period is 100 ms.  Parameters start from the offline default `(0,2,1)` and the
   AP begins controlling after the 1 s warmup.

The asymmetric hold times deliberately make overload protection fast and return toward a more
aggressive setting slow.  A minimum DS-CTS sample count prevents one failed burst in a small
sample from appearing as a 25-50% collision rate.

## Smooth-traffic promotion (design, not validated in this experiment)

QSRC 0 should be a separate, evidence-gated state, not the next step below QSRC 2.  Promotion
would require a multi-second window showing all of the following:

- low cross-station simultaneous backlog (from BSR), low arrival-count Fano factor and no
  coherent queue peaks;
- stable reservation yield and low Stage-1 loss;
- no worsening in deadline delivery during short, reversible q0 probes;
- enough P-EDCA stations for the NAV/reservation network effect to matter.

Any synchronized backlog peak, rising Stage-1 cost, or falling probe reward returns immediately
to the bursty-safe state.  This prevents an On/Off burst from being mistaken for CBR merely
because the AP PHY has idle/backoff time.  It also reflects that the current CBR q0 advantage is
an empirical regime result, not a protocol invariant.

## Validation arms

- EDCA only;
- offline default P-EDCA `(CWds,QSRC,PSRC)=(0,2,1)`;
- offline best `(0,4,3)`, selected by the existing 10-run dual-DS On/Off sweep using R(10 ms);
- adaptive P-EDCA starting from `(0,2,1)` and using `burstadaptive`.

The comparison uses nSta=30, On/Off mean offered load 1 Mbps/STA, nPEDCA in `{5,15,30}`, three
paired random seeds, an 8 s run and a 1 s measurement warmup.
