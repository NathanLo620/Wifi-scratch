# P-EDCA under Light Load: Tuning CWds × QSRC × PSRC (802.11be, re-run v6.3.2, 2026-07-18)
### nSta = 30, offered load 0.1 / 0.5 Mbps per STA (vs 1 Mbps saturated reference) — P-EDCA STA P99
> Data vintage: re-simulated on v6.3.2 code (EHT NAV / frame-exchange fixes), PHY rate-matched to
> 11n (EhtMcs5 @ GI 1.6 µs = 65.0 Mbps). Deck: `11be/pedca_lightload_insights{,_en}.pdf`.

---

**The three load regimes (EDCA-only VO P99 sets the scene)**
- **0.1 Mbps — uncongested**: EDCA-only P99 = 0.92 ms (sub-ms). Nothing for P-EDCA to fix.
- **0.5 Mbps — congested, not saturated**: EDCA-only P99 = 21.96 ms. Real queueing, drainable.
- **1.0 Mbps — saturated**: EDCA-only P99 = 46.2 ms. Collision-feedback regime.

---

**Key Insight 1 — P-EDCA's value tracks congestion, and now RISES with penetration**
- 0.1 Mbps: best +5–7% only (0.87 vs 0.92 ms) — effectively a no-op
- 0.5 Mbps: +56% (n=5) → +75% (n=15) → **+87%** (n=30): 22.0 → 2.9 ms
- 1.0 Mbps: +50% / +44% / **+71%** (n=5/15/30): 46.2 → 13.2 ms
- (Pre-v6.3 data showed the gain *collapsing* at high penetration — that reversed.)

**Key Insight 2 — one aggressive recipe wins at every load with real queueing**
- QSRC strictly monotonic at 0.5 Mbps (q0 5.8 → q5 25.8 ms) AND at saturation (q0 22.2 → q5 48.8 ms)
- PSRC larger-is-better at both (0.5 Mbps: s3 12.7 vs s1 18.7 ms; sat: s3 36.4 vs s1 41.4 ms)
- CWds don't-care. Recipe: **QSRC = 0, PSRC = 3, CWds = 0/1** — no load-dependent backoff needed anymore

**Key Insight 3 — at 0.1 Mbps, don't trigger P-EDCA at all**
- Shallow reverse-U in QSRC: q0 is mildly the WORST (1.4 ms) vs q2 (1.0 ms)
- Winning combo drifts across nPedca (c0q5s2 / c1q3s1 / c0q2s3) — params don't matter when uncongested

**Key Insight 4 — the old saturation tail blow-up is GONE**
- Worst combo over the 36: 0.1 Mbps 1.4 ms; 0.5 Mbps 28 ms; 1.0 Mbps 53 ms ≈ the EDCA-only baseline
- Worst case now means "no benefit", never "blow-up"
- The pre-v6.3 pathology (QSRC 0/1 + PSRC 3 at saturation, n=5 → P99 up to 294 ms) no longer
  reproduces after the v6.3.1/v6.3.2 NAV & frame-exchange fixes — it was a bug artifact, not a
  property of aggressive parameters

---

**Recommendations (minimize P99)**
| Load regime | CWds | QSRC | PSRC |
|-------------|------|------|------|
| 0.1 Mbps (uncongested) | any | large (don't trigger) | any |
| 0.5 Mbps (congested) | 0/1 | 0 | 3 |
| 1.0 Mbps (saturated) | 0/1 | 0 | 3 |

**Bottom line:** P-EDCA is a congestion tool — worthless below the queueing knee, and worth
+87% / +71% P99 above it. With the v6.3.x fixes the aggressive recipe is safe everywhere,
so the only decision left is *whether* to trigger (QSRC), not *how carefully*.
