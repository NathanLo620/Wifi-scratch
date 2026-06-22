# Tuning P-EDCA Parameters to Minimize P99 VO Delay
### nSta = 30, 1 Mbps — sweep over CWds × QSRC × PSRC × nPedca

---

**What the knobs do**
- **CWds** — Stage-1 contention window after entering P-EDCA (0 = ASAP, 1 = random[0,1]): collision control
- **QSRC** — short-retry threshold that *triggers* P-EDCA: **how early** a STA enters the priority path
- **PSRC** — max *consecutive* priority attempts before kickback to EDCA: **how long** it stays prioritized
- Channel airtime is **zero-sum**: P-EDCA gains are paid for by Legacy STAs

---

**Key Insight 1 — PSRC is the dominant lever (and a trade-off)**
- Larger PSRC → P-EDCA STA P99 drops sharply (qsrc=0, nPedca=5: 15.9k → 8.0k → 5.6k µs)
- Measured P-EDCA airtime share rises 62% → 74% → 87% as PSRC 1 → 2 → 3
- Same airtime is taken from Legacy → Legacy P99 *rises* (19.4k → 20.5k → 20.8k µs)
- **All-STA view ≈ flat**: P-EDCA gain cancels Legacy loss (except nPedca=30, no Legacy)

**Key Insight 2 — "smaller QSRC" only pays off when PSRC ≥ 2**
- Early trigger helps only if the priority path can actually drain the packet
- PSRC=1: one shot then kickback → QSRC barely matters (even slightly reversed)
- PSRC≥2: small QSRC (0–2) best; QSRC 3–5 wastes slow EDCA retries first → tail explodes

**Key Insight 3 — CWds and the q1 wiggle are second-order**
- CWds=1 mildly better (de-synchronizes simultaneous P-EDCA STAs); |corr| < 0.2
- Legacy/All QSRC ripples are within Monte-Carlo noise — not a real trend

**Key Insight 4 — nPedca modulates everything**
- High nPedca → priority channel self-congests (attempt success 62% → 43%) → benefits saturate

---

**Recommendations (minimize P99)**
| Goal | CWds | QSRC | PSRC |
|------|------|------|------|
| P-EDCA STAs | 1 | 0–1 | 3 (larger) |
| Legacy STAs | 1 | 0 | 1 (smaller) |
| All STAs (balanced) | 1 | 0 | ~1 (gains cancel) |

**Bottom line:** PSRC sets *how much* airtime P-EDCA seizes from Legacy; QSRC sets *how early*,
but only cashes in when PSRC ≥ 2; CWds is fine-tuning — all gated by P-EDCA-channel congestion (nPedca).
