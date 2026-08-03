#!/usr/bin/env python3
"""
P-EDCA under light load: best-P99 parameter combos and comparison vs saturated load.
Builds a 3-page deck (English + Traditional Chinese) from the current sweeps:

  fix_nsta30_CwdsxQSRCxPSRC_sweep_lightload/combo_percentile_summary_{0.1,0.5}Mbps.csv
  fix_nsta30_CwdsxQSRCxPSRC_sweep/combo_percentile_summary_1Mbps.csv          (saturated = CBR 1 Mbps)
  <dir>/edca_only/edca_only_p00_vo_delay_pdf_nSta30_<rate>.csv                (EDCA-only VO P99)

Data vintage (IMPORTANT): sweeps re-run 2026-08-02 on top of v6.4.1 "Dual DS-CTS mechanism" +
the v6.3.3-v6.3.5 NAV/CF-End fixes. Stage 1 now sends the DS-CTS twice (SIFS apart) and Stage 2
contends after SIFS instead of AIFS -- this is a DIFFERENT protocol from the single-DS-CTS data
this deck previously reported (2026-07-19). Numbers and the QSRC/PSRC direction both changed;
see scratch/delay_pdf/11be memory notes ("dual-dscts-findings", "pedca-adaptive-policy") for the
full derivation. Do not average or compare these numbers against the pre-2026-07-28 deck.

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
    import statistics as st
    return dict(combo=f"c{best['CWds']} q{best['QSRC']} s{best['PSRC']}",
                cwds=best["CWds"], qsrc=best["QSRC"], psrc=best["PSRC"],
                P99=float(best["P99_us"]) / 1000,
                byQ=marg("QSRC"), byS=marg("PSRC"),
                med=st.median(float(r["P99_us"]) for r in sub) / 1000,
                worst=max(float(r["P99_us"]) for r in sub) / 1000)

D = {}; EDCA = {}
for key, label, csvp, bdir, rate, col in LOADS:
    rws = rows_of(csvp)
    D[key] = {n: analyse(rws, "pedca", n) for n in NPEDCAS}
    EDCA[key] = edca_p99_ms(bdir, rate)

def gain(key, n):
    """positive = P-EDCA better than EDCA-only; negative = P-EDCA worse."""
    e = EDCA[key]; return (e - D[key][n]["P99"]) / e * 100 if e else 0

# k-driven QSRC/PSRC law derived from the 2026-08-02 full CwdsxQSRCxPSRC rerun (see memory
# "pedca-adaptive-policy"): CWds=1 fixed, QSRC scales up and PSRC scales down as k grows.
def klaw(k):
    return min(5, max(0, round(0.5 + 0.2 * k))), (3 if k <= 10 else (2 if k <= 22 else 1))

# ───────────────────────── helpers ─────────────────────────
def L(en, zh, lang): return zh if lang == "zh" else en
def wrap_lines(t, w, lang):
    if lang == "zh":
        ww = max(6, w // 2); return [t[i:i + ww] for i in range(0, len(t), ww)]
    return textwrap.wrap(t, width=w)
def footer(fig, page, lang):
    fig.text(0.045, 0.035, L("ns-3.45 P-EDCA light-load sweep | scratch/delay_pdf/11be | Dual DS-CTS (v6.4.1) re-run, 2026-08-02",
                             "ns-3.45 P-EDCA 輕載掃描 | scratch/delay_pdf/11be | Dual DS-CTS(v6.4.1)重跑，2026-08-02", lang),
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
        L("P-EDCA under light load, Dual DS-CTS: fixes low-k timing, costs high-k airtime",
          "輕載下的 P-EDCA(Dual DS-CTS)：修好了低滲透率的時序，卻在高滲透率付出空中時間代價", lang),
        L("802.11be, nSta=30, CWds×QSRC×PSRC sweep | offered load 0.1 / 0.5 / 1.0 Mbps per STA | P-EDCA STA P99",
          "802.11be，nSta=30，CWds×QSRC×PSRC 掃描 | 每 STA 負載 0.1 / 0.5 / 1.0 Mbps | P-EDCA STA P99", lang))

    fig.text(0.045, 0.805, L("The three load regimes (EDCA-only VO P99 sets the scene)",
                             "三種負載區間(以純 EDCA VO P99 定調)", lang),
             fontsize=13, fontweight="bold", color=INK)
    reg = [
        (GREENL, L("0.1 Mbps — uncongested", "0.1 Mbps — 未壅塞", lang),
         L(f"EDCA-only P99 = {EDCA['0.1']:.2f} ms (sub-ms). Little for P-EDCA to fix.",
           f"純 EDCA P99 = {EDCA['0.1']:.2f} ms(次毫秒)。P-EDCA 幾乎無事可修。", lang)),
        (BLUE, L("0.5 Mbps — congested, not saturated", "0.5 Mbps — 壅塞但未飽和", lang),
         L(f"EDCA-only P99 = {EDCA['0.5']:.2f} ms. Real queueing; whether P-EDCA helps now depends on k.",
           f"純 EDCA P99 = {EDCA['0.5']:.2f} ms。已有排隊；P-EDCA 是否有幫助現在要看 k。", lang)),
        (ORANGE, L("1.0 Mbps — saturated", "1.0 Mbps — 飽和", lang),
         L(f"EDCA-only P99 = {EDCA['1.0']:.2f} ms. Far lower than the pre-fix baseline (NAV/CF-End bugs fixed).",
           f"純 EDCA P99 = {EDCA['1.0']:.2f} ms。遠低於修正前的基準(NAV/CF-End bug 已修)。", lang)),
    ]
    y = 0.745
    for col, h, b in reg:
        fig.text(0.055, y, "■", color=col, fontsize=13, va="center")
        fig.text(0.075, y, h, fontsize=10.5, fontweight="bold", color=INK, va="center")
        fig.text(0.075, y - 0.032, b, fontsize=9.3, color="#444", va="center")
        y -= 0.082

    card(fig, 0.60, 0.66, 0.35, 0.15,
         L(f"0.1 Mbps: +{min(gain('0.1',n) for n in NPEDCAS):.0f}~+{max(gain('0.1',n) for n in NPEDCAS):.0f}%",
           f"0.1 Mbps：+{min(gain('0.1',n) for n in NPEDCAS):.0f}~+{max(gain('0.1',n) for n in NPEDCAS):.0f}%", lang),
         L("modest but consistently positive at every k", "每個 k 都有小幅正向增益", lang),
         L(f"n=15 weakest (+{gain('0.1',15):.0f}%) — winning combo still drifts, params matter less here",
           f"n=15 最弱(+{gain('0.1',15):.0f}%) — 最佳組合仍會飄移，代表參數在此不太重要", lang), bc=GREEN)
    card(fig, 0.60, 0.475, 0.35, 0.15,
         L("0.5 & 1.0 Mbps: sign flips at n=30", "0.5、1.0 Mbps：n=30 時符號翻轉", lang),
         L(f"n=5 +{gain('0.5',5):.0f}%/+{gain('1.0',5):.0f}%  →  n=30 {gain('0.5',30):+.1f}%/{gain('1.0',30):+.1f}%",
           f"n=5 +{gain('0.5',5):.0f}%/+{gain('1.0',5):.0f}%  →  n=30 {gain('0.5',30):+.1f}%/{gain('1.0',30):+.1f}%", lang),
         L("full penetration: P-EDCA now COSTS more than it saves", "全滲透時：P-EDCA 現在花費比它省下的還多", lang), bc=RED)
    card(fig, 0.60, 0.29, 0.35, 0.15,
         L("Recipe must now scale with k", "配方現在必須隨 k 調整", lang),
         L("QSRC rises, PSRC falls as k grows (opposite of pre-Dual)", "k 越大 QSRC 越大、PSRC 越小(跟 Dual 之前相反)", lang),
         L("one fixed combo can't cover k=5..30 any more", "單一固定組合已無法涵蓋 k=5~30", lang), bs=18)

    ax = fig.add_axes([0.045, 0.10, 0.52, 0.15]); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.0, 0.0), 1, 1, boxstyle="round,pad=0.01,rounding_size=0.03",
                 fc="#FAFAF9", ec=CARDBD, lw=1, transform=ax.transAxes))
    ax.text(0.04, 0.80, L("One-line takeaway", "一句話結論", lang), fontsize=11, fontweight="bold",
            color=INK, transform=ax.transAxes)
    ax.text(0.04, 0.40, L("Dual DS-CTS fixed the old low-k timing-miss failure, so P-EDCA now helps at k=5-15\n"
                          "everywhere. But the DS-CTS reservation is exclusive (one window, one transmitter),\n"
                          "so cost scales with k while benefit doesn't -- at k=30 it now costs more than it saves.",
                          "Dual DS-CTS 修好了舊版低滲透率下的時序失誤，P-EDCA 在 k=5-15 現在到處都有幫助。\n"
                          "但 DS-CTS 保留是排他性的(一個窗口只服務一個傳送者)，成本隨 k 增加、效益卻沒有，\n"
                          "k=30 時現在反而得不償失。", lang),
            fontsize=9.4, color="#333", va="center", linespacing=1.5, transform=ax.transAxes)
    footer(fig, 1, lang); pdf.savefig(fig); plt.close(fig)

# ───────────────────────── page 2 ─────────────────────────
def page2(pdf, lang):
    fig = plt.figure(figsize=(13.33, 7.5)); fig.patch.set_facecolor("white")
    title_block(fig,
        L("Best P-EDCA P99 across loads — gain peaks at low-moderate penetration",
          "各負載下最佳 P-EDCA P99 — 增益現在於低~中滲透率達到高峰", lang),
        L("EDCA-only baseline vs best-of-36-combos P-EDCA (P-EDCA STAs, n=5), with % P99 change",
          "純 EDCA 基準 vs 36 組合最佳 P-EDCA(P-EDCA STA，n=5)，標示 P99 變化", lang))

    ax = fig.add_axes([0.07, 0.40, 0.42, 0.42])
    xs = range(len(LOADS))
    base = [EDCA[k] for k, *_ in LOADS]
    best = [D[k][5]["P99"] for k, *_ in LOADS]
    ax.bar([x - 0.2 for x in xs], base, 0.4, color=GREY, label=L("EDCA-only", "純 EDCA", lang))
    ax.bar([x + 0.2 for x in xs], best, 0.4, color=[c for *_, c in LOADS], label=L("best P-EDCA (n=5)", "最佳 P-EDCA(n=5)", lang))
    for i, (k, *_ ) in enumerate(LOADS):
        g = gain(k, 5)
        lbl = f"+{g:.0f}%" if g >= 0 else f"{g:.0f}%"
        ax.text(i + 0.2, best[i] + max(base) * 0.02, lbl, ha="center", fontsize=10,
                fontweight="bold", color=(GREEN if g >= 0 else RED))
        ax.text(i - 0.2, base[i] + max(base) * 0.02, f"{base[i]:.1f}", ha="center", fontsize=8, color=MUTED)
    ax.set_xticks(list(xs)); ax.set_xticklabels([lbl for _, lbl, *_ in LOADS], fontsize=9)
    ax.set_ylabel(L("VO P99 delay (ms)", "VO P99 延遲 (ms)", lang), fontsize=10)
    ax.set_title(L("P99: EDCA-only vs best P-EDCA (n=5)", "P99：純 EDCA vs 最佳 P-EDCA(n=5)", lang),
                 fontsize=11.5, fontweight="bold", color=INK)
    ax.legend(fontsize=9, frameon=False); ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25); ax.set_ylim(0, max(base + best) * 1.2)

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
            a = D[k][n]; g = gain(k, n)
            fig.text(cx[0], y, lbl if j == 0 else "", fontsize=8.6, color=col, fontweight="bold")
            fig.text(cx[1], y, f"{n}", fontsize=8.6, color="#333")
            fig.text(cx[2], y, a["combo"], fontsize=8.6, color="#333")
            fig.text(cx[3], y, f"{a['P99']:.2f}  ({g:+.0f}%)", fontsize=8.6,
                     color=(INK if g >= 0 else RED))
            y -= 0.028
        y -= 0.010

    bullets = [
        L(f"Gain no longer rises monotonically with penetration -- it PEAKS at low-moderate k and "
          f"reverses at full penetration: 0.5 Mbps +{gain('0.5',5):.0f}% (n=5) → +{gain('0.5',15):.0f}% (n=15) "
          f"→ {gain('0.5',30):+.1f}% (n=30); 1.0 Mbps +{gain('1.0',5):.0f}% → +{gain('1.0',15):.0f}% → {gain('1.0',30):+.1f}%.",
          f"增益不再隨滲透率單調上升 -- 而是在低~中 k 達到高峰、全滲透時翻負：0.5 Mbps "
          f"+{gain('0.5',5):.0f}%(n=5) → +{gain('0.5',15):.0f}%(n=15) → {gain('0.5',30):+.1f}%(n=30)；"
          f"1.0 Mbps +{gain('1.0',5):.0f}% → +{gain('1.0',15):.0f}% → {gain('1.0',30):+.1f}%。", lang),
        L("Why: the DS-CTS reservation is exclusive -- one protected window serves exactly one "
          "transmitter, but every P-EDCA STA pays the DS-CTS airtime cost to compete for it. Benefit "
          "per STA scales ~1/k while cost scales ~k, so net value crosses zero as k grows.",
          "原因：DS-CTS 保留是排他性的 -- 一個受保護窗口只服務一個傳送者，但每個 P-EDCA STA 都要付出 "
          "DS-CTS 空中時間成本去競爭它。每個 STA 的效益隨 k 遞減(~1/k)、成本卻隨 k 遞增(~k)，"
          "淨值因此隨 k 增加而穿越零點。", lang),
        L(f"At 0.1 Mbps gain stays small and positive at every k (+{min(gain('0.1',n) for n in NPEDCAS):.0f}"
          f"~+{max(gain('0.1',n) for n in NPEDCAS):.0f}%) -- uncongested traffic never reaches the "
          f"exclusivity bottleneck, so the reservation cost stays affordable.",
          f"0.1 Mbps 下增益在每個 k 都維持小幅正值(+{min(gain('0.1',n) for n in NPEDCAS):.0f}~"
          f"+{max(gain('0.1',n) for n in NPEDCAS):.0f}%) -- 未壅塞流量從未觸及排他性瓶頸，保留成本仍能負擔。", lang),
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
        L("Which knob matters at which k — QSRC and PSRC reversed direction",
          "哪個旋鈕在哪個 k 重要 — QSRC 與 PSRC 現在的方向跟以前相反", lang),
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
        ax.set_title(L(f"P99 vs {knob} (n=30)", f"P99 對 {knob}(n=30)", lang), fontsize=11, fontweight="bold", color=INK)
        ax.set_xticks(list(D["0.5"][30][key].keys()))
        ax.spines[["top", "right"]].set_visible(False); ax.grid(alpha=0.25); ax.tick_params(labelsize=8)

    # k-driven recipe callout (replaces the old "tail blow-up gone" box -- that pathology no
    # longer applies; the live issue now is the exclusivity cost documented on page 2)
    ax = fig.add_axes([0.70, 0.44, 0.26, 0.36]); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0.01,rounding_size=0.04",
                 fc="#F1F5FA", ec="#C9D9EA", lw=1, transform=ax.transAxes))
    ax.text(0.5, 0.92, L("k-driven recipe", "k-驅動配方", lang), ha="center", fontsize=11.5,
            fontweight="bold", color=INK, transform=ax.transAxes)
    ax.text(0.5, 0.78, L("CWds = 1 fixed; QSRC/PSRC scale with k", "CWds=1 固定；QSRC/PSRC 隨 k 調整", lang),
            ha="center", fontsize=8.3, color=MUTED, transform=ax.transAxes)
    yy = 0.63
    for k in NPEDCAS:
        q, s = klaw(k)
        ax.text(0.5, yy, f"k={k:<2d}  ->  QSRC={q}, PSRC={s}", ha="center", fontsize=9.7,
                fontweight="bold", color=INK, transform=ax.transAxes)
        yy -= 0.135
    ax.text(0.5, 0.10, L("law: QSRC=round(0.5+0.2k), PSRC=3/2/1\nfor k<=10 / <=22 / >22",
                         "公式：QSRC=round(0.5+0.2k)，\nPSRC=3/2/1 對應 k<=10/<=22/>22", lang),
            ha="center", fontsize=7.6, color=MUTED, transform=ax.transAxes)

    bullets = [
        L(f"QSRC direction REVERSED from the pre-Dual deck: at n=30, LOW QSRC is now worst "
          f"(q0 {D['0.5'][30]['byQ'][0]:.1f} ms) and HIGH QSRC is best (q5 {D['0.5'][30]['byQ'][5]:.1f} ms) "
          f"at 0.5 Mbps -- q0 keeps every STA retrying into the exclusive window; higher QSRC filters "
          f"out most of the excess demand before it pays the DS-CTS cost.",
          f"QSRC 方向跟 Dual 之前的版本相反：0.5 Mbps 在 n=30 時，低 QSRC 現在反而最差"
          f"(q0 {D['0.5'][30]['byQ'][0]:.1f} ms)、高 QSRC 最好(q5 {D['0.5'][30]['byQ'][5]:.1f} ms) -- "
          f"q0 讓每個 STA 都不斷重試搶那個排他窗口；QSRC 較高則能在付出 DS-CTS 成本前先篩掉多餘需求。", lang),
        L(f"PSRC direction also reversed at high k: s1 {D['0.5'][30]['byS'][1]:.1f} ms now beats "
          f"s3 {D['0.5'][30]['byS'][3]:.1f} ms at n=30 -- fewer consecutive attempts per winner leaves "
          f"more of the shared airtime for everyone else. At n=5 the OLD direction still holds "
          f"(PSRC=3 better) since exclusivity barely bites when only 5 STAs compete.",
          f"高 k 時 PSRC 方向也反過來了：n=30 時 s1 {D['0.5'][30]['byS'][1]:.1f} ms 現在贏過 "
          f"s3 {D['0.5'][30]['byS'][3]:.1f} ms -- 贏家連續嘗試次數變少，留給其他人的共用空中時間變多。"
          f"n=5 時舊方向仍成立(PSRC=3 較好)，因為只有 5 個 STA 競爭時排他性幾乎不構成問題。", lang),
        L("Recommendation: stop using one fixed combo. Estimate k (number of STAs actually using "
          "P-EDCA) and apply the k-driven law above; getting k badly wrong costs more than getting "
          "traffic type wrong (see memory \"pedca-adaptive-policy\" for the full cost table and the "
          "closed-loop controller that automates this).",
          "建議：不要再用單一固定組合。估計 k(實際使用 P-EDCA 的 STA 數)並套用上方 k-驅動公式；"
          "k 估錯的代價比流量型態估錯還大(完整成本表與自動化此流程的閉環控制器見 memory "
          "「pedca-adaptive-policy」)。", lang),
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
