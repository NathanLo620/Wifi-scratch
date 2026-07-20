#!/usr/bin/env python3
"""
P-EDCA under light load: best-P99 parameter combos and comparison vs saturated load.
Builds a 3-page deck (English + Traditional Chinese) from the current sweeps:

  fix_nsta30_CwdsxQSRCxPSRC_sweep_lightload/combo_percentile_summary_{0.1,0.5}Mbps.csv
  fix_nsta30_CwdsxQSRCxPSRC_sweep/combo_percentile_summary_1Mbps.csv          (saturated = CBR 1 Mbps)
  <dir>/edca_only/edca_only_p00_vo_delay_pdf_nSta30_<rate>.csv                (EDCA-only VO P99)

Perspective = P-EDCA STAs (delay_type 'pedca'), metric = P99 VO delay.
Outputs (in this 11be/ dir):
  pedca_lightload_insights_en.pdf , pedca_lightload_insights.pdf
"""
import csv, textwrap
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch
import matplotlib.font_manager as fm

BASE = Path(__file__).resolve().parent
LL   = BASE / "fix_nsta30_CwdsxQSRCxPSRC_sweep_lightload"
SAT  = BASE / "fix_nsta30_CwdsxQSRCxPSRC_sweep"
NPEDCAS = [5, 15, 30]

# load key -> (label, summary_csv, baseline_dir, rate, colour)
GREENL = "#4FA45A"; BLUE = "#3B79D6"; ORANGE = "#E8743B"
LOADS = [
    ("0.1", "0.1 Mbps/STA", LL / "combo_percentile_summary_0.1Mbps.csv", LL,  "0.1Mbps", GREENL),
    ("0.5", "0.5 Mbps/STA", LL / "combo_percentile_summary_0.5Mbps.csv", LL,  "0.5Mbps", BLUE),
    ("1.0", "1 Mbps/STA (sat.)", SAT / "combo_percentile_summary_1Mbps.csv", SAT, "1Mbps", ORANGE),
]
INK = "#232323"; MUTED = "#6B6B6B"; FAINT = "#9A9A9A"; GREEN = "#2E7D32"; RED = "#C0392B"
CARDBG = "#F4F3F1"; CARDBD = "#E2E0DC"; GREY = "#B7B4AE"

# ───────────────────────── data ─────────────────────────
def rows_of(csvp):
    seen = set(); out = []
    for r in csv.DictReader(open(csvp)):        # summaries are 2x-duplicated after resume; dedupe
        k = (r["CWds"], r["QSRC"], r["PSRC"], r["nPedca"], r["delay_type"])
        if k in seen: continue
        seen.add(k); out.append(r)
    return out

def edca_p99_ms(bdir, rate):
    p = bdir / "edca_only" / f"edca_only_p00_vo_delay_pdf_nSta30_{rate}.csv"
    mids = []; probs = []
    for r in csv.DictReader(open(p)):
        mids.append((float(r["bin_start_us"]) + float(r["bin_end_us"])) / 2 / 1000)
        probs.append(float(r["probability"]))
    tot = sum(probs) or 1; run = 0
    for m, pr in zip(mids, probs):
        run += pr
        if run / tot >= 0.99: return m
    return mids[-1] if mids else 0

def analyse(rows, dt, n):
    sub = [r for r in rows if r["delay_type"] == dt and int(r["nPedca"]) == n]
    if not sub: return None
    best = min(sub, key=lambda r: float(r["P99_us"]))
    def marg(key):
        g = {}
        for r in sub: g.setdefault(int(r[key]), []).append(float(r["P99_us"]) / 1000)
        return {k: sum(v) / len(v) for k, v in sorted(g.items())}
    return dict(combo=f"c{best['CWds']} q{best['QSRC']} s{best['PSRC']}",
                cwds=best["CWds"], qsrc=best["QSRC"], psrc=best["PSRC"],
                P99=float(best["P99_us"]) / 1000,
                byQ=marg("QSRC"), byS=marg("PSRC"),
                worst=max(float(r["P99_us"]) for r in sub) / 1000)

D = {}; EDCA = {}
for key, label, csvp, bdir, rate, col in LOADS:
    rws = rows_of(csvp)
    D[key] = {n: analyse(rws, "pedca", n) for n in NPEDCAS}
    EDCA[key] = edca_p99_ms(bdir, rate)

def gain(key, n):
    e = EDCA[key]; return (e - D[key][n]["P99"]) / e * 100 if e else 0

# ───────────────────────── helpers ─────────────────────────
def L(en, zh, lang): return zh if lang == "zh" else en
def wrap_lines(t, w, lang):
    if lang == "zh":
        ww = max(6, w // 2); return [t[i:i + ww] for i in range(0, len(t), ww)]
    return textwrap.wrap(t, width=w)
def footer(fig, page, lang):
    fig.text(0.045, 0.035, L("ns-3.45 P-EDCA light-load sweep | scratch/delay_pdf/11be | re-run on v6.3.2, 2026-07-19",
                             "ns-3.45 P-EDCA 輕載掃描 | scratch/delay_pdf/11be | v6.3.2 重跑，2026-07-19", lang),
             fontsize=7.5, color=FAINT, va="center")
    fig.text(0.955, 0.035, f"{page} / 3", fontsize=7.5, color=FAINT, va="center", ha="right")
def title_block(fig, t, s):
    fig.text(0.045, 0.93, t, fontsize=20.5, fontweight="bold", color=INK, va="center")
    fig.text(0.045, 0.876, s, fontsize=11.5, color=MUTED, va="center")
def card(fig, x, y, w, h, big, sub, small, bc=INK, bs=23):
    ax = fig.add_axes([x, y, w, h]); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.02, 0.05), 0.96, 0.9, boxstyle="round,pad=0.01,rounding_size=0.05",
                 fc=CARDBG, ec=CARDBD, lw=1, transform=ax.transAxes))
    ax.text(0.5, 0.72, big, ha="center", va="center", fontsize=bs, fontweight="bold", color=bc, transform=ax.transAxes)
    ax.text(0.5, 0.42, sub, ha="center", va="center", fontsize=10.3, color=INK, transform=ax.transAxes)
    ax.text(0.5, 0.20, small, ha="center", va="center", fontsize=8.1, color=MUTED, transform=ax.transAxes)

# ───────────────────────── page 1 ─────────────────────────
def page1(pdf, lang):
    fig = plt.figure(figsize=(13.33, 7.5)); fig.patch.set_facecolor("white")
    title_block(fig,
        L("P-EDCA under light load: best P99 parameters vs saturated load",
          "輕載下的 P-EDCA：最佳 P99 參數組合與飽和負載之比較", lang),
        L("802.11be, nSta=30, CWds×QSRC×PSRC sweep | offered load 0.1 / 0.5 / 1.0 Mbps per STA | P-EDCA STA P99",
          "802.11be，nSta=30，CWds×QSRC×PSRC 掃描 | 每 STA 負載 0.1 / 0.5 / 1.0 Mbps | P-EDCA STA P99", lang))

    fig.text(0.045, 0.805, L("The three load regimes (EDCA-only VO P99 sets the scene)",
                             "三種負載區間(以純 EDCA VO P99 定調)", lang),
             fontsize=13, fontweight="bold", color=INK)
    reg = [
        (GREENL, L("0.1 Mbps — uncongested", "0.1 Mbps — 未壅塞", lang),
         L(f"EDCA-only P99 = {EDCA['0.1']:.2f} ms (sub-ms). Nothing for P-EDCA to fix.",
           f"純 EDCA P99 = {EDCA['0.1']:.2f} ms(次毫秒)。P-EDCA 無事可修。", lang)),
        (BLUE, L("0.5 Mbps — congested, not saturated", "0.5 Mbps — 壅塞但未飽和", lang),
         L(f"EDCA-only P99 = {EDCA['0.5']:.2f} ms. Real queueing but the priority path can drain it.",
           f"純 EDCA P99 = {EDCA['0.5']:.2f} ms。已有排隊，但優先路徑排得掉。", lang)),
        (ORANGE, L("1.0 Mbps — saturated", "1.0 Mbps — 飽和", lang),
         L(f"EDCA-only P99 = {EDCA['1.0']:.2f} ms. Collision-feedback regime.",
           f"純 EDCA P99 = {EDCA['1.0']:.2f} ms。碰撞回饋區間。", lang)),
    ]
    y = 0.745
    for col, h, b in reg:
        fig.text(0.055, y, "■", color=col, fontsize=13, va="center")
        fig.text(0.075, y, h, fontsize=10.5, fontweight="bold", color=INK, va="center")
        fig.text(0.075, y - 0.032, b, fontsize=9.3, color="#444", va="center")
        y -= 0.082

    card(fig, 0.60, 0.66, 0.35, 0.15,
         L("0.1 Mbps: ≈ no-op", "0.1 Mbps：形同無作用", lang),
         L("P-EDCA cuts P99 only +5–7%", "P-EDCA 只降 P99 +5–7%", lang),
         L(f"best {D['0.1'][30]['P99']:.2f} ms vs EDCA {EDCA['0.1']:.2f} ms — any combo works (q0 even slightly worst)",
           f"最佳 {D['0.1'][30]['P99']:.2f} ms vs EDCA {EDCA['0.1']:.2f} ms — 任何組合皆可(q0 反而略差)", lang), bc=GREEN)
    card(fig, 0.60, 0.475, 0.35, 0.15,
         L("0.5 Mbps: sweet spot", "0.5 Mbps：甜蜜點", lang),
         L(f"+{gain('0.5',30):.0f}% P99 ({EDCA['0.5']:.1f} → {D['0.5'][30]['P99']:.1f} ms)",
           f"+{gain('0.5',30):.0f}% P99({EDCA['0.5']:.1f} → {D['0.5'][30]['P99']:.1f} ms)", lang),
         L(f"QSRC=0, PSRC=3 — monotonic; worst combo ≤ {max(D['0.5'][n]['worst'] for n in NPEDCAS):.0f} ms",
           f"QSRC=0、PSRC=3 — 單調；最差組合 ≤ {max(D['0.5'][n]['worst'] for n in NPEDCAS):.0f} ms", lang), bc=GREEN)
    card(fig, 0.60, 0.29, 0.35, 0.15,
         L("One recipe at every load", "各負載同一配方", lang),
         L("QSRC = 0,  PSRC = 3,  CWds = 0/1", "QSRC = 0、PSRC = 3、CWds = 0/1", lang),
         L("now wins at saturation too — the old n=5 tail blow-up is gone (v6.3.x fixes)",
           "飽和時同樣勝出 — 舊的 n=5 尾端爆炸已消失(v6.3.x 修正)", lang), bs=18)

    ax = fig.add_axes([0.045, 0.10, 0.52, 0.15]); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0.01,rounding_size=0.03",
                 fc="#FAFAF9", ec=CARDBD, lw=1, transform=ax.transAxes))
    ax.text(0.04, 0.80, L("One-line takeaway", "一句話結論", lang), fontsize=11, fontweight="bold",
            color=INK, transform=ax.transAxes)
    ax.text(0.04, 0.40, L(f"P-EDCA's value tracks how congested EDCA already is: near-zero at 0.1 Mbps,\n"
                          f"+{gain('0.5',30):.0f}% at 0.5 Mbps and +{gain('1.0',30):.0f}% at saturation (n=30). The aggressive\n"
                          f"recipe (QSRC 0, PSRC 3) wins at every load with real queueing — no tail risk left.",
                          f"P-EDCA 的價值取決於 EDCA 本身有多壅塞：0.1 Mbps 幾乎為零、\n"
                          f"0.5 Mbps +{gain('0.5',30):.0f}%、飽和 +{gain('1.0',30):.0f}%(n=30)。只要有實質排隊，\n"
                          f"積極配方(QSRC 0、PSRC 3)在每種負載都勝出 — 尾端風險已不存在。", lang),
            fontsize=9.4, color="#333", va="center", linespacing=1.5, transform=ax.transAxes)
    footer(fig, 1, lang); pdf.savefig(fig); plt.close(fig)

# ───────────────────────── page 2 ─────────────────────────
def page2(pdf, lang):
    fig = plt.figure(figsize=(13.33, 7.5)); fig.patch.set_facecolor("white")
    title_block(fig,
        L("Best P-EDCA P99 across loads — the gain peaks at moderate load",
          "各負載下最佳 P-EDCA P99 — 增益在中等負載達到高峰", lang),
        L("EDCA-only baseline vs best-of-36-combos P-EDCA (P-EDCA STAs, n=30), with % P99 reduction",
          "純 EDCA 基準 vs 36 組合最佳 P-EDCA(P-EDCA STA，n=30)，標示 P99 降幅", lang))

    ax = fig.add_axes([0.07, 0.40, 0.42, 0.42])
    xs = range(len(LOADS))
    base = [EDCA[k] for k, *_ in LOADS]
    best = [D[k][30]["P99"] for k, *_ in LOADS]
    ax.bar([x - 0.2 for x in xs], base, 0.4, color=GREY, label=L("EDCA-only", "純 EDCA", lang))
    ax.bar([x + 0.2 for x in xs], best, 0.4, color=[c for *_, c in LOADS], label=L("best P-EDCA", "最佳 P-EDCA", lang))
    for i, (k, *_ ) in enumerate(LOADS):
        ax.text(i + 0.2, best[i] + 0.4, f"−{gain(k,30):.0f}%", ha="center", fontsize=10,
                fontweight="bold", color=GREEN)
        ax.text(i - 0.2, base[i] + 0.4, f"{base[i]:.1f}", ha="center", fontsize=8, color=MUTED)
    ax.set_xticks(list(xs)); ax.set_xticklabels([lbl for _, lbl, *_ in LOADS], fontsize=9)
    ax.set_ylabel(L("VO P99 delay (ms)", "VO P99 延遲 (ms)", lang), fontsize=10)
    ax.set_title(L("P99: EDCA-only vs best P-EDCA (n=30)", "P99：純 EDCA vs 最佳 P-EDCA(n=30)", lang),
                 fontsize=11.5, fontweight="bold", color=INK)
    ax.legend(fontsize=9, frameon=False); ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25); ax.set_ylim(0, max(base) * 1.15)

    # detail table (load × nPedca)
    fig.text(0.56, 0.79, L("Best combo & P99 per P-EDCA population",
                           "各 P-EDCA STA 數的最佳組合與 P99", lang), fontsize=11.5, fontweight="bold", color=INK)
    cx = [0.565, 0.66, 0.775, 0.885]
    hdr = [L("Load", "負載", lang), L("nPedca", "nPedca", lang),
           L("best c/q/s", "最佳 c/q/s", lang), L("P99 (ms)", "P99 (ms)", lang)]
    for h, x in zip(hdr, cx): fig.text(x, 0.755, h, fontsize=9, fontweight="bold", color=INK)
    y = 0.725
    for k, lbl, *_ in LOADS:
        col = [c for kk, _, _, _, _, c in LOADS if kk == k][0]
        for j, n in enumerate(NPEDCAS):
            a = D[k][n]
            fig.text(cx[0], y, lbl if j == 0 else "", fontsize=8.6, color=col, fontweight="bold")
            fig.text(cx[1], y, f"{n}", fontsize=8.6, color="#333")
            fig.text(cx[2], y, a["combo"], fontsize=8.6, color="#333")
            fig.text(cx[3], y, f"{a['P99']:.2f}  (−{gain(k,n):.0f}%)", fontsize=8.6, color="#333")
            y -= 0.028
        y -= 0.010

    bullets = [
        L(f"Relative gain grows with congestion: +5–7% at 0.1 Mbps → +{gain('0.5',30):.0f}% at 0.5 Mbps → "
          f"+{gain('1.0',30):.0f}% at saturation (n=30). P-EDCA only helps once EDCA itself is congested.",
          f"相對增益隨壅塞增加：0.1 Mbps 僅 +5–7% → 0.5 Mbps +{gain('0.5',30):.0f}% → "
          f"飽和 +{gain('1.0',30):.0f}%(n=30)。EDCA 本身壅塞後 P-EDCA 才有用。", lang),
        L(f"At 0.1 Mbps the best P99 ({D['0.1'][30]['P99']:.2f} ms) barely beats EDCA ({EDCA['0.1']:.2f} ms) and the "
          f"winning combo drifts across nPedca ({D['0.1'][5]['combo']} / {D['0.1'][15]['combo']} / {D['0.1'][30]['combo']}) "
          f"— a sign params don't matter when uncongested.",
          f"0.1 Mbps 下最佳 P99({D['0.1'][30]['P99']:.2f} ms)幾乎追平 EDCA({EDCA['0.1']:.2f} ms)，"
          f"且最佳組合隨 nPedca 飄移({D['0.1'][5]['combo']} / {D['0.1'][15]['combo']} / {D['0.1'][30]['combo']})— "
          f"代表未壅塞時參數不重要。", lang),
        L(f"At 0.5 & 1.0 Mbps the winner is consistently aggressive (QSRC 0–1, PSRC 3), and the gain RISES with "
          f"penetration: 0.5 Mbps +{gain('0.5',5):.0f}% → +{gain('0.5',15):.0f}% → +{gain('0.5',30):.0f}%; "
          f"saturation +{gain('1.0',5):.0f}% / +{gain('1.0',15):.0f}% / +{gain('1.0',30):.0f}% (n=5/15/30).",
          f"0.5 與 1.0 Mbps 下最佳者一致偏積極(QSRC 0–1、PSRC 3)，且增益隨滲透率上升：0.5 Mbps "
          f"+{gain('0.5',5):.0f}% → +{gain('0.5',15):.0f}% → +{gain('0.5',30):.0f}%；"
          f"飽和 +{gain('1.0',5):.0f}% / +{gain('1.0',15):.0f}% / +{gain('1.0',30):.0f}%(n=5/15/30)。", lang),
    ]
    y = 0.315
    for b in bullets:
        fig.text(0.05, y, "•", fontsize=11, color=BLUE, va="top")
        for i, ln in enumerate(wrap_lines(b, 118, lang)):
            fig.text(0.068, y - i * 0.025, ln, fontsize=8.7, color="#333", va="top")
        y -= 0.025 * (len(wrap_lines(b, 118, lang)) + 0.5)
    footer(fig, 2, lang); pdf.savefig(fig); plt.close(fig)

# ───────────────────────── page 3 ─────────────────────────
def page3(pdf, lang):
    fig = plt.figure(figsize=(13.33, 7.5)); fig.patch.set_facecolor("white")
    title_block(fig,
        L("Which knob matters at which load — and where the old tail-risk went",
          "哪個旋鈕在哪種負載重要 — 以及舊的尾端風險去哪了", lang),
        L("Marginal mean P99 (P-EDCA STAs, n=30) vs QSRC and vs PSRC, per load",
          "各負載下邊際平均 P99(P-EDCA STA，n=30)對 QSRC 及 PSRC 的關係", lang))

    for idx, (knob, key) in enumerate([("QSRC", "byQ"), ("PSRC", "byS")]):
        ax = fig.add_axes([0.07 + idx * 0.34, 0.44, 0.27, 0.36])
        for k, lbl, _, _, _, col in LOADS:
            m = D[k][30][key]
            ax.plot(list(m.keys()), list(m.values()), marker="o", ms=4, color=col, lw=1.8, label=lbl)
        ax.set_xlabel(knob, fontsize=10)
        if idx == 0:
            ax.set_ylabel(L("mean P99 (ms)", "平均 P99 (ms)", lang), fontsize=10)
            ax.legend(fontsize=8.2, frameon=False)
        ax.set_title(L(f"P99 vs {knob}", f"P99 對 {knob}", lang), fontsize=11, fontweight="bold", color=INK)
        ax.set_xticks(list(D["0.5"][30][key].keys()))
        ax.spines[["top", "right"]].set_visible(False); ax.grid(alpha=0.25); ax.tick_params(labelsize=8)

    # worst-case callout (the old blow-up no longer reproduces)
    ax = fig.add_axes([0.70, 0.44, 0.26, 0.36]); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0.01,rounding_size=0.04",
                 fc="#F1F7F1", ec="#C6DEC6", lw=1, transform=ax.transAxes))
    ax.text(0.5, 0.90, L("Tail blow-up: gone", "尾端爆炸：已消失", lang), ha="center", fontsize=11.5,
            fontweight="bold", color=GREEN, transform=ax.transAxes)
    w05 = max(D["0.5"][n]["worst"] for n in NPEDCAS)
    w10 = max(D["1.0"][n]["worst"] for n in NPEDCAS)
    ax.text(0.5, 0.62, L("worst-case P99 over 36 combos", "36 組合中最差 P99", lang),
            ha="center", fontsize=9, color=INK, transform=ax.transAxes)
    ax.text(0.5, 0.47, f"0.1: {max(D['0.1'][n]['worst'] for n in NPEDCAS):.1f} ms   "
                       f"0.5: {w05:.0f} ms   1.0: {w10:.0f} ms", ha="center", fontsize=9.5, color=INK,
            transform=ax.transAxes)
    ax.text(0.5, 0.30, L("worst ≈ EDCA-only baseline\n(= no benefit, never a blow-up)",
                         "最差 ≈ 純 EDCA 基準\n(= 沒有好處，但不會爆掉)", lang),
            ha="center", fontsize=9.5, fontweight="bold", color=INK, transform=ax.transAxes)
    ax.text(0.5, 0.10, L("the pre-v6.3 294 ms n=5 blow-up no longer\nreproduces after the NAV/FEM fixes",
                         "v6.3 之前的 294 ms(n=5)爆炸\n在 NAV/FEM 修正後不再重現", lang),
            ha="center", fontsize=8, color=MUTED, transform=ax.transAxes)

    bullets = [
        L(f"QSRC: at 0.1 Mbps a shallow reverse-U (q0 is mildly the WORST, {D['0.1'][30]['byQ'][0]:.1f} vs "
          f"{D['0.1'][30]['byQ'][2]:.1f} ms at q2 — don't trigger P-EDCA when uncongested); at 0.5 Mbps strongly "
          f"monotonic (q0 {D['0.5'][30]['byQ'][0]:.1f} ms vs q5 {D['0.5'][30]['byQ'][5]:.1f} ms); at 1.0 Mbps "
          f"now ALSO monotonic (q0 {D['1.0'][30]['byQ'][0]:.1f} ms vs q5 {D['1.0'][30]['byQ'][5]:.1f} ms).",
          f"QSRC：0.1 Mbps 呈淺淺的倒 U(q0 反而最差，{D['0.1'][30]['byQ'][0]:.1f} vs q2 {D['0.1'][30]['byQ'][2]:.1f} ms — "
          f"未壅塞時別觸發 P-EDCA)；0.5 Mbps 強烈單調(q0 {D['0.5'][30]['byQ'][0]:.1f} ms vs q5 "
          f"{D['0.5'][30]['byQ'][5]:.1f} ms)；1.0 Mbps 現在也單調(q0 {D['1.0'][30]['byQ'][0]:.1f} ms vs q5 "
          f"{D['1.0'][30]['byQ'][5]:.1f} ms)。", lang),
        L(f"PSRC: irrelevant at 0.1 Mbps; clearly larger-is-better at 0.5 Mbps "
          f"(s3 {D['0.5'][30]['byS'][3]:.1f} ms vs s1 {D['0.5'][30]['byS'][1]:.1f} ms) and at saturation "
          f"(s3 {D['1.0'][30]['byS'][3]:.1f} ms vs s1 {D['1.0'][30]['byS'][1]:.1f} ms) — with the v6.3.x fixes "
          f"PSRC=3 no longer carries any blow-up risk.",
          f"PSRC：0.1 Mbps 無關緊要；0.5 Mbps 明顯越大越好"
          f"(s3 {D['0.5'][30]['byS'][3]:.1f} ms vs s1 {D['0.5'][30]['byS'][1]:.1f} ms)，"
          f"飽和亦然(s3 {D['1.0'][30]['byS'][3]:.1f} ms vs s1 {D['1.0'][30]['byS'][1]:.1f} ms) — "
          f"v6.3.x 修正後 PSRC=3 不再有爆炸風險。", lang),
        L(f"Robust recommendation — one recipe at every load with real queueing: QSRC=0, PSRC=3, CWds=0/1 "
          f"(worst-case ≤ {w05:.0f} ms at 0.5 Mbps, ≤ {w10:.0f} ms at saturation ≈ the EDCA-only baseline). "
          f"At 0.1 Mbps simply leave P-EDCA untriggered (large QSRC) — there is nothing to gain.",
          f"穩健建議 — 只要有實質排隊，各負載同一配方：QSRC=0、PSRC=3、CWds=0/1"
          f"(最差 ≤ {w05:.0f} ms @0.5 Mbps、≤ {w10:.0f} ms @飽和 ≈ 純 EDCA 基準)。"
          f"0.1 Mbps 則讓 P-EDCA 不觸發(大 QSRC)即可 — 沒有可得的增益。", lang),
    ]
    y = 0.335
    for b in bullets:
        fig.text(0.05, y, "•", fontsize=11, color=BLUE, va="top")
        lns = wrap_lines(b, 120, lang)
        for i, ln in enumerate(lns):
            fig.text(0.068, y - i * 0.024, ln, fontsize=8.6, color="#333", va="top")
        y -= 0.024 * (len(lns) + 0.6)
    footer(fig, 3, lang); pdf.savefig(fig); plt.close(fig)

# ───────────────────────── run ─────────────────────────
def build(lang, out):
    if lang == "zh":
        for nm in ("Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans CJK SC"):
            if any(nm in f.name for f in fm.fontManager.ttflist):
                plt.rcParams["font.family"] = nm; break
    else:
        plt.rcParams["font.family"] = "DejaVu Sans"
    plt.rcParams["axes.unicode_minus"] = False
    with PdfPages(BASE / out) as pdf:
        page1(pdf, lang); page2(pdf, lang); page3(pdf, lang)
    print("wrote", out)

if __name__ == "__main__":
    build("en", "pedca_lightload_insights_en.pdf")
    build("zh", "pedca_lightload_insights.pdf")
