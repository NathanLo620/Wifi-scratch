# Tuning P-EDCA Parameters to Minimize P99 VO Delay (802.11be, re-run v6.3.2, 2026-07-18)
### nSta = 30, 1 Mbps (saturated), CBR — sweep over CWds × QSRC × PSRC × nPedca
> Data vintage: this directory was re-simulated on v6.3.2 code (EHT NAV / frame-exchange fixes),
> with the PHY rate-matched to 11n (EhtMcs5 @ GI 1.6 µs = 65.0 Mbps). Numbers below replace the
> pre-v6.3 results; several old conclusions flipped.

---

**What the knobs do**
- **CWds** — Stage-1 contention window after entering P-EDCA (0 = ASAP, 1 = random[0,1]): collision control
- **QSRC** — short-retry threshold that *triggers* P-EDCA: **how early** a STA enters the priority path
- **PSRC** — max *consecutive* priority attempts before kickback to EDCA: **how long** it stays prioritized
- Baseline for scale: EDCA-only (nPedca = 0) VO P99 = **46.2 ms**; best P-EDCA combo reaches **13.2 ms** (+71%)

---

**Key Insight 1 — PSRC is still the dominant lever, but it is no longer zero-sum**
- Larger PSRC → P-EDCA STA P99 drops sharply (q0 avg: n=5 42.9 → 35.3 → 24.6 ms; n=30 28.5 → 24.9 → 13.2 ms)
- Measured P-EDCA airtime share rises 59% → 80% → 87% as PSRC 1 → 2 → 3 (q0, n=5)
- **Legacy STAs no longer pay**: Legacy P99 (q0, n=15) 37.8 → 36.4 → 35.3 ms — it *improves* slightly.
  Draining VO faster reduces overall congestion instead of just stealing airtime (contrast with pre-v6.3 data).

**Key Insight 2 — small QSRC now pays off at every PSRC (monotonic everywhere)**
- PSRC=1, n=30: q0 28.5 → q5 46.3 ms; PSRC=3: q0 13.2 → q5 52.1 ms — strictly increasing in QSRC
- The old "QSRC only matters when PSRC ≥ 2" interaction is gone; early trigger always helps,
  but the payoff roughly doubles when PSRC=3 lets the priority path finish the job

**Key Insight 3 — CWds is negligible**
- c0 vs c1 average P99 differs < 1% at every nPedca (39.2/39.0, 40.5/40.8, 39.5/39.2 ms)

**Key Insight 4 — nPedca: priority self-congestion is real but no longer fatal**
- P-EDCA attempt success (q0 s3) falls 59% (n=5) → 37% (n=30)
- Yet n=30 is the BEST slice (13.2 ms vs 24.6 ms at n=5): with everyone inside the scheme,
  DS-CTS ordering replaces CWmin=3 collision contention system-wide
- Cross-standard context: fully-penetrated 11be beats the best 11n by 22–46% at P99;
  at n=5/15 11be still trails 11n (its EDCA baseline is ~2.1–2.4× worse when rate-matched)

---

**Recommendations (minimize P99, saturated load)**
| Goal | CWds | QSRC | PSRC |
|------|------|------|------|
| P-EDCA STAs | any | 0 | 3 |
| Legacy STAs | any | 0 | 3 (they benefit too) |
| All STAs | any | 0 | 3 |

**Bottom line:** one aggressive recipe — QSRC 0, PSRC 3, CWds don't-care — now wins for every
view. PSRC sets how effectively the priority path drains the queue; QSRC=0 triggers it immediately;
and on v6.3.2 the drained congestion helps Legacy as well, so there is no longer a trade-off to balance.
