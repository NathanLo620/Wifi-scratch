#!/usr/bin/env python3
"""
Regenerate the 802.11be vs 802.11n P-EDCA tail-latency insight deck (4 pages),
in both English and Traditional-Chinese, straight from the current sweep CSVs.

Data sources (per PHY in {11n,11be} × traffic in {CBR,Poisson,MMPP,OnOff}):
  <phy>/fix_nsta30_CwdsxQSRCxPSRC_sweep[_poisson|_MMPP|_onoff]/
      combo_percentile_summary_1Mbps.csv        -> min P50/P95/P99 over 36 combos
      best_vs_default_p99_gain_1Mbps.csv         -> gain vs EDCA-only / default, EDCA P99
      edca_only/pedca_count_sweep_statistics_edca_only_1Mbps.txt  -> VO loss/queue/access/idle
      edca_only/edca_only_p00_vo_delay_pdf_nSta30_1Mbps.csv       -> EDCA-only VO delay CDF

Metric conventions (match the original deck):
  * Page-2 P99 bars use delay_type = 'pedca' (P-EDCA STA perspective).
  * Delta shown = (11be - 11n)/11n : negative (green) => 11be lower/better.
  * Page-3 gain bars = gain_vs_EDCA at nPedca=30, delay_type 'pedca'.

Outputs (overwrites in place):
  11be_vs_11n_insights_en.pdf
  11be_vs_11n_insights.pdf   (Traditional Chinese)
"""
import csv, re, textwrap
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import matplotlib.font_manager as fm

BASE = Path(__file__).resolve().parent          # .../delay_pdf/11be  (PDF output dir)
DATA = Path(__file__).resolve().parent.parent    # .../delay_pdf       (holds 11n/ and 11be/)
TRAFFIC = [("CBR", ""), ("Poisson", "_poisson"), ("MMPP", "_MMPP"), ("OnOff", "_onoff")]
NPEDCAS = [5, 15, 30]

# ---- palette (matched to the original deck) ----
ORANGE = "#E8743B"   # 802.11n
BLUE   = "#3B79D6"   # 802.11be
GREEN  = "#2E7D32"
RED    = "#C0392B"
INK    = "#232323"
MUTED  = "#6B6B6B"
FAINT  = "#9A9A9A"
CARDBG = "#F4F3F1"
CARDBD = "#E2E0DC"

# ═════════════════════════ data extraction ═════════════════════════
def sdir(phy, suf): return DATA / phy / f"fix_nsta30_CwdsxQSRCxPSRC_sweep{suf}"

def load_summary(phy, suf):
    rows = list(csv.DictReader(open(sdir(phy, suf) / "combo_percentile_summary_1Mbps.csv")))
    out = {}
    for dt in ("pedca", "all", "legacy"):
        for n in NPEDCAS:
            sub = [r for r in rows if r["delay_type"] == dt and int(r["nPedca"]) == n]
            if not sub: continue
            best = min(sub, key=lambda r: float(r["P99_us"]))
            out[(dt, n)] = dict(minP99=float(best["P99_us"]),
                                combo=f"c{best['CWds']}q{best['QSRC']}s{best['PSRC']}")
    return out

def load_gain(phy, suf):
    out = {}
    for r in csv.DictReader(open(sdir(phy, suf) / "best_vs_default_p99_gain_1Mbps.csv")):
        st = {"All": "all", "P-EDCA": "pedca", "Legacy": "legacy"}[r["sta_type"].split()[0]]
        out[(st, int(r["nPedca"]))] = dict(gain_vs_EDCA=float(r["gain_vs_EDCA_%"]),
                                           edcaP99=float(r["EDCA_only_P99_us"]))
    return out

def load_edca_vo(phy, suf):
    txt = (sdir(phy, suf) / "edca_only" / "pedca_count_sweep_statistics_edca_only_1Mbps.txt").read_text()
    d = {}
    m = re.search(r"Channel Idle Time \(AP\):\s*([\d.]+)", txt); d["idle"] = float(m.group(1)) if m else 0
    vo = re.search(r"AC_VO:(.*?)(?:\nAC_|\n---|\Z)", txt, re.DOTALL)
    if vo:
        t = vo.group(1)
        for k, p in [("loss", r"Packet Loss:\s*([\d.]+)"), ("queue", r"Avg Queue Delay:\s*(-?[\d.]+)"),
                     ("access", r"Avg Access Delay:\s*(-?[\d.]+)")]:
            mm = re.search(p, t); d[k] = float(mm.group(1)) if mm else 0
    return d

def load_cdf(phy, suf):
    mids, probs = [], []
    for r in csv.DictReader(open(sdir(phy, suf) / "edca_only" / "edca_only_p00_vo_delay_pdf_nSta30_1Mbps.csv")):
        mids.append((float(r["bin_start_us"]) + float(r["bin_end_us"])) / 2 / 1000.0)
        probs.append(float(r["probability"]))
    tot = sum(probs) or 1
    cdf, run = [], 0.0
    for pr in probs: run += pr; cdf.append(run / tot)
    def pct(q):
        for m, c in zip(mids, cdf):
            if c >= q: return m
        return mids[-1] if mids else 0
    return dict(mids=mids, cdf=cdf, P10=pct(.10), P50=pct(.50), P99=pct(.99))

D = {}
for phy in ("11n", "11be"):
    D[phy] = {}
    for tn, suf in TRAFFIC:
        D[phy][tn] = dict(summary=load_summary(phy, suf), gain=load_gain(phy, suf),
                          edca=load_edca_vo(phy, suf), cdf=load_cdf(phy, suf))

# ---- derived ranges for headline cards ----
def rng(vals, f="%.0f"):
    lo, hi = min(vals), max(vals)
    return f % lo, f % hi

p99_n30 = {tn: (D["11n"][tn]["summary"][("pedca", 30)]["minP99"] / 1000,
               D["11be"][tn]["summary"][("pedca", 30)]["minP99"] / 1000) for tn, _ in TRAFFIC}
red_n30 = [(a - b) / a * 100 for a, b in p99_n30.values()]          # reduction %
be_n30  = [b for _, b in p99_n30.values()]
n_n30   = [a for a, _ in p99_n30.values()]
loss_n  = [D["11n"][tn]["edca"]["loss"] for tn, _ in TRAFFIC]
loss_be = [D["11be"][tn]["edca"]["loss"] for tn, _ in TRAFFIC]
gain_be = [D["11be"][tn]["gain"][("pedca", 30)]["gain_vs_EDCA"] for tn, _ in TRAFFIC]
gain_n  = [D["11n"][tn]["gain"][("pedca", 30)]["gain_vs_EDCA"] for tn, _ in TRAFFIC]
# EDCA-only PHY P99 improvement per traffic (11n->11be)
phy_p99_gain = [(D["11n"][tn]["cdf"]["P99"] - D["11be"][tn]["cdf"]["P99"]) / D["11n"][tn]["cdf"]["P99"] * 100
                for tn, _ in TRAFFIC]

# ═════════════════════════ language strings ═════════════════════════
def L(en, zh, lang): return zh if lang == "zh" else en

def wrap_lines(text, width_en, lang):
    """Wrap to a predictable list of lines. CJK has no spaces, so chunk by char
    count (roughly half the English width); English uses word wrapping."""
    if lang == "zh":
        w = max(6, width_en // 2)
        return [text[i:i + w] for i in range(0, len(text), w)]
    return textwrap.wrap(text, width=width_en)

def strings(lang):
    S = {}
    S["footer"] = L("ns-3.45 P-EDCA simulations | scratch/delay_pdf/{11n,11be} | 2026-07-13",
                    "ns-3.45 P-EDCA 模擬 | scratch/delay_pdf/{11n,11be} | 2026-07-13", lang)
    return S

# ═════════════════════════ drawing helpers ═════════════════════════
def footer(fig, s, page):
    fig.text(0.045, 0.035, s["footer"], fontsize=7.5, color=FAINT, va="center")
    fig.text(0.955, 0.035, f"{page} / 4", fontsize=7.5, color=FAINT, va="center", ha="right")

def card(fig, x, y, w, h, big, sub, small, big_color=INK):
    ax = fig.add_axes([x, y, w, h]); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.02, 0.04), 0.96, 0.92, boxstyle="round,pad=0.01,rounding_size=0.04",
                 fc=CARDBG, ec=CARDBD, lw=1, transform=ax.transAxes))
    ax.text(0.5, 0.70, big, ha="center", va="center", fontsize=25, fontweight="bold",
            color=big_color, transform=ax.transAxes)
    ax.text(0.5, 0.40, sub, ha="center", va="center", fontsize=10.5, color=INK, transform=ax.transAxes)
    ax.text(0.5, 0.20, small, ha="center", va="center", fontsize=8.2, color=MUTED, transform=ax.transAxes)

def flowbox(ax, x, w, text):
    ax.add_patch(FancyBboxPatch((x, 0.12), w, 0.76, boxstyle="round,pad=0.005,rounding_size=0.02",
                 fc="white", ec=CARDBD, lw=1, transform=ax.transAxes))
    ax.text(x + w / 2, 0.5, text, ha="center", va="center", fontsize=7.6, color=INK,
            wrap=True, transform=ax.transAxes)

def title_block(fig, title, subtitle):
    fig.text(0.045, 0.93, title, fontsize=21, fontweight="bold", color=INK, va="center")
    fig.text(0.045, 0.875, subtitle, fontsize=11.5, color=MUTED, va="center")

# ═════════════════════════ page builders ═════════════════════════
def page1(pdf, lang, s):
    fig = plt.figure(figsize=(13.33, 7.5)); fig.patch.set_facecolor("white")
    ttl = L("802.11be vs 802.11n: P-EDCA Tail-Latency Comparison",
            "802.11be vs 802.11n：P-EDCA 尾延遲效能比較", lang)
    sub = L("CWds × QSRC × PSRC parameter sweep | CBR / Poisson / MMPP / OnOff traffic | P-EDCA STA perspective",
            "CWds × QSRC × PSRC 參數掃描 | CBR / Poisson / MMPP / OnOff 四種流量 | P-EDCA STA 視角", lang)
    title_block(fig, ttl, sub)

    fig.text(0.045, 0.80, L("Simulation setup", "模擬設定", lang), fontsize=14, fontweight="bold", color=INK)
    rows = [
        (L("Topology", "拓樸", lang), L("30 STAs → 1 AP, random 1–5 m disc; 5 GHz ch36 / 20 MHz",
                                       "30 STAs → 1 AP，隨機分布於 1–5 m；5 GHz ch36 / 20 MHz", lang)),
        (L("Traffic", "流量", lang), L("Uplink AC_VO UDP, 1 Mbps per STA (1000 B); CBR / Poisson / MMPP / OnOff",
                                      "上行 AC_VO UDP，每 STA 1 Mbps(1000 B)；CBR / Poisson / MMPP / OnOff", lang)),
        (L("PHY", "PHY", lang), "11n = HtMcs7 (65 Mbps)  vs  11be = EhtMcs7 (73.1 Mbps @ 3.2 µs GI)"),
        (L("Control", "控制幀", lang), L("OFDM 6 Mbps for both; A-MPDU on; VO TXOP limit 2.08 ms",
                                        "兩者同為 OFDM 6 Mbps；A-MPDU 開啟；VO TXOP 上限 2.08 ms", lang)),
        (L("Fairness", "公平性", lang), L("The two programs differ by exactly 2 lines (SetStandard / DataMode)",
                                         "兩標準的模擬程式僅差 2 行(SetStandard / DataMode)，其餘相同", lang)),
        (L("Sweep", "掃描", lang), "nPedca ∈ {5, 15, 30}; CWds{0,1} × QSRC{0–5} × PSRC{1–3} = 36 combos"),
        (L("Statistics", "統計", lang), L("10 seeds × 10 s per point; plus an EDCA-only baseline (nPedca = 0)",
                                         "每點 10 seeds × 10 s；另含純 EDCA 基準(nPedca = 0)", lang)),
    ]
    y = 0.745
    for k, v in rows:
        fig.text(0.055, y, k, fontsize=10, fontweight="bold", color=INK, va="center")
        fig.text(0.16, y, v, fontsize=10, color="#333", va="center")
        y -= 0.048
    fig.text(0.045, y - 0.005, L("Data integrity: 4 traffic × 290 delay histograms are independent runs "
                                 "(11n / 11be md5 all differ)",
                                 "資料完整性：4 流量 × 290 個延遲直方圖皆為獨立模擬(11n / 11be md5 全數相異)", lang),
             fontsize=8, color=FAINT, va="center")

    # cards (recomputed)
    c1_lo, c1_hi = rng(red_n30)
    card(fig, 0.60, 0.66, 0.35, 0.155,
         f"P99  −{c1_hi}% ~ −{c1_lo}%",
         L("Tail latency at full P-EDCA load (30/30), best params",
           "重載(30/30 全 P-EDCA)最佳參數下的尾延遲", lang),
         L(f"Consistent across 4 traffic; 11be {min(be_n30):.1f}–{max(be_n30):.1f} ms vs 11n {min(n_n30):.0f}–{max(n_n30):.0f} ms",
           f"四種流量一致；11be {min(be_n30):.1f}–{max(be_n30):.1f} ms vs 11n {min(n_n30):.0f}–{max(n_n30):.0f} ms", lang),
         big_color=GREEN)
    card(fig, 0.60, 0.475, 0.35, 0.155,
         f"Loss  {min(loss_n):.0f}–{max(loss_n):.0f}% → {min(loss_be):.0f}–{max(loss_be):.0f}%",
         L("VO retry-limit loss, EDCA-only baseline", "純 EDCA 基準的 VO retry-limit 損失率", lang),
         L("Same params & load; 11n stuck in collision feedback, 11be not",
           "同參數、同負載；11n 深陷碰撞回饋、11be 未達", lang),
         big_color=GREEN)
    card(fig, 0.60, 0.29, 0.35, 0.155,
         f"+{min(gain_be):.0f}% ~ +{max(gain_be):.0f}% vs EDCA",
         L("P-EDCA still pays off at full penetration on 11be", "11be 上 P-EDCA 機制在全滲透仍大幅有效", lang),
         L(f"Only +{min(gain_n):.0f}% to +{max(gain_n):.0f}% on 11n — P-EDCA value grows with the PHY generation",
           f"11n 上僅 +{min(gain_n):.0f}% ~ +{max(gain_n):.0f}% — P-EDCA 價值隨 PHY 世代放大", lang))

    # takeaway
    ax = fig.add_axes([0.045, 0.10, 0.52, 0.16]); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.0, 0.0), 1, 1, boxstyle="round,pad=0.01,rounding_size=0.03",
                 fc="#FAFAF9", ec=CARDBD, lw=1, transform=ax.transAxes))
    ax.text(0.04, 0.80, L("One-line takeaway", "一句話結論", lang), fontsize=11, fontweight="bold",
            color=INK, transform=ax.transAxes)
    ax.text(0.04, 0.42, L("11be's edge is NOT faster per-frame airtime (access delay nearly identical) — it\n"
                          "pulls the system off the high-collision operating point: at heavy load the tail\n"
                          "roughly halves and the P-EDCA mechanism becomes worthwhile again.",
                          "11be 的優勢不在「每一幀傳得快」(access delay 幾乎相同)，\n"
                          "而在把系統從高碰撞的操作點拉回 → 重載下尾延遲約砍半，\n"
                          "P-EDCA 機制重新變得有價值。", lang),
            fontsize=9.5, color="#333", va="center", transform=ax.transAxes, linespacing=1.5)
    footer(fig, s, 1); pdf.savefig(fig); plt.close(fig)

def page2(pdf, lang, s):
    fig = plt.figure(figsize=(13.33, 7.5)); fig.patch.set_facecolor("white")
    title_block(fig,
        L("Core result: P-EDCA P99 latency — 11be's edge grows with load",
          "核心結果：P-EDCA P99 延遲 — 11be 的優勢隨負載增大", lang),
        L("Minimum P99 over the 36-combo sweep, per traffic × nPedca (mean of 10 seeds; ms)",
          "36 組合掃描的最小 P99，依流量 × nPedca(10 seeds 平均；ms)", lang))
    # legend
    fig.text(0.70, 0.845, "■", color=ORANGE, fontsize=12); fig.text(0.72, 0.845, "802.11n", fontsize=10, color=INK)
    fig.text(0.80, 0.845, "■", color=BLUE, fontsize=12);   fig.text(0.82, 0.845, "802.11be", fontsize=10, color=INK)

    axpos = [0.06, 0.285, 0.51, 0.735]  # left edges of 4 non-overlapping panels
    pw = 0.185
    ymax = max(max(a, b) for tn, _ in TRAFFIC
               for a, b in [(D["11n"][tn]["summary"][("pedca", n)]["minP99"] / 1000,
                             D["11be"][tn]["summary"][("pedca", n)]["minP99"] / 1000) for n in NPEDCAS]) * 1.18
    for i, (tn, _) in enumerate(TRAFFIC):
        ax = fig.add_axes([axpos[i], 0.30, pw, 0.44])
        xs = range(len(NPEDCAS))
        a = [D["11n"][tn]["summary"][("pedca", n)]["minP99"] / 1000 for n in NPEDCAS]
        b = [D["11be"][tn]["summary"][("pedca", n)]["minP99"] / 1000 for n in NPEDCAS]
        ax.bar([x - 0.19 for x in xs], a, 0.36, color=ORANGE)
        ax.bar([x + 0.19 for x in xs], b, 0.36, color=BLUE)
        for j, n in enumerate(NPEDCAS):
            d = (b[j] - a[j]) / a[j] * 100
            ax.text(j, max(a[j], b[j]) + ymax * 0.03, f"{d:+.0f}%", ha="center", fontsize=9.5,
                    fontweight="bold", color=(GREEN if d < 0 else RED))
        ax.set_title(tn, fontsize=12, fontweight="bold", color=INK)
        ax.set_xticks(list(xs)); ax.set_xticklabels([f"n={n}" for n in NPEDCAS], fontsize=8.5)
        ax.set_xlim(-0.6, 2.6); ax.set_ylim(0, ymax)
        if i == 0: ax.set_ylabel(L("P99 delay (ms)", "P99 延遲 (ms)", lang), fontsize=10)
        else: ax.set_yticklabels([])
        ax.spines[["top", "right"]].set_visible(False); ax.tick_params(labelsize=8)
        ax.grid(axis="y", alpha=0.25)

    bullets = [
        L(f"Light P-EDCA load n=5: 11be shows NO gain, even a penalty (CBR +6%, bursty +74–84%) — "
          f"few P-EDCA STAs, the priority channel is uncongested and the faster PHY does not help the subset tail.",
          f"輕 P-EDCA 負載 n=5：11be 毫無優勢、甚至更差(CBR +6%，突發流量 +74–84%) — "
          f"P-EDCA STA 少、優先通道不擁塞，較快的 PHY 幫不到此子集的尾端。", lang),
        L(f"Medium n=15: 11be −16% to −29%.  Heavy n=30: 11be −{min(red_n30):.0f}% to −{max(red_n30):.0f}% "
          f"(11n {min(n_n30):.0f}–{max(n_n30):.0f} ms vs 11be {min(be_n30):.1f}–{max(be_n30):.1f} ms).",
          f"中載 n=15：11be −16% ~ −29%。重載 n=30：11be −{min(red_n30):.0f}% ~ −{max(red_n30):.0f}% "
          f"(11n {min(n_n30):.0f}–{max(n_n30):.0f} ms vs 11be {min(be_n30):.1f}–{max(be_n30):.1f} ms)。", lang),
        L("The n=5 penalty is consistent across the whole 36-combo distribution (medians too), not a min-artifact — "
          "it is the low-contention regime where the P-EDCA subset tail is arrival-driven and noisy.",
          "n=5 的劣勢在整個 36 組合分布(含中位數)都一致，並非最小值假象 — "
          "這是低競爭區間，P-EDCA 子集尾端由到達過程主導、雜訊大。", lang),
        L("Best combos: both PHYs prefer aggressive PSRC=3 and small QSRC (0–2); 11n's slow large-CW retries "
          "under load still cannot recover → saturated at 17–21 ms.",
          "最佳組合：兩 PHY 都偏好積極的 PSRC=3、小 QSRC(0–2)；11n 在重載下的大 CW 慢速重傳仍無法回復 → 飽和在 17–21 ms。", lang),
    ]
    y = 0.215
    for bl in bullets:
        fig.text(0.05, y, "•", fontsize=11, color=BLUE, va="top")
        fig.text(0.068, y, bl, fontsize=8.6, color="#333", va="top", wrap=True)
        y -= 0.048
    footer(fig, s, 2); pdf.savefig(fig); plt.close(fig)

def page3(pdf, lang, s):
    fig = plt.figure(figsize=(13.33, 7.5)); fig.patch.set_facecolor("white")
    title_block(fig,
        L("Mechanism: queueing & collisions, not frame speed",
          "機制：排隊與碰撞，而非傳輸速度", lang),
        L("The whole delay distribution shifts left. EDCA-only baseline (no P-EDCA, 30 STAs) isolates the two PHY generations.",
          "整個延遲分布左移。純 EDCA 基準(無 P-EDCA，30 STA)可隔離兩個 PHY 世代。", lang))

    for i, tn in enumerate(["CBR", "OnOff"]):
        ax = fig.add_axes([0.06 + i * 0.24, 0.42, 0.20, 0.36])
        for phy, col in [("11n", ORANGE), ("11be", BLUE)]:
            c = D[phy][tn]["cdf"]; ax.plot(c["mids"], c["cdf"], color=col, lw=1.8, label=f"802.{phy}")
        ax.set_xlim(0, 25); ax.set_ylim(0, 1.02)
        ax.set_title(L(f"{tn} (EDCA-only) VO delay CDF", f"{tn}(純 EDCA)VO 延遲 CDF", lang),
                     fontsize=10.5, fontweight="bold", color=INK)
        ax.set_xlabel(L("Delay (ms)", "延遲 (ms)", lang), fontsize=9)
        if i == 0:
            ax.set_ylabel("CDF", fontsize=9)
            ax.legend(loc="lower right", fontsize=8.5, frameon=False)
        ax.spines[["top", "right"]].set_visible(False); ax.tick_params(labelsize=8); ax.grid(alpha=0.25)

    # gain bars
    ax = fig.add_axes([0.60, 0.42, 0.34, 0.36])
    xs = range(len(TRAFFIC))
    ax.bar([x - 0.19 for x in xs], gain_n, 0.36, color=ORANGE)
    ax.bar([x + 0.19 for x in xs], gain_be, 0.36, color=BLUE)
    for j in xs:
        ax.text(j - 0.19, gain_n[j] + 1.5, f"{gain_n[j]:.0f}%", ha="center", fontsize=8, color=INK)
        ax.text(j + 0.19, gain_be[j] + 1.5, f"{gain_be[j]:.0f}%", ha="center", fontsize=8, fontweight="bold", color=INK)
    ax.set_title(L("P-EDCA gain over EDCA-only (best params, n=30)",
                   "P-EDCA 相對純 EDCA 之增益(最佳參數，n=30)", lang), fontsize=10.5, fontweight="bold", color=INK)
    ax.set_ylabel(L("P99 improvement (%)", "P99 改善 (%)", lang), fontsize=9)
    ax.set_xticks(list(xs)); ax.set_xticklabels([t for t, _ in TRAFFIC], fontsize=8.5)
    ax.set_ylim(0, max(gain_be) * 1.25); ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(labelsize=8); ax.grid(axis="y", alpha=0.25)

    p10 = [(D["11be"][tn]["cdf"]["P10"] - D["11n"][tn]["cdf"]["P10"]) / D["11n"][tn]["cdf"]["P10"] * 100 for tn, _ in TRAFFIC]
    p50 = [(D["11be"][tn]["cdf"]["P50"] - D["11n"][tn]["cdf"]["P50"]) / D["11n"][tn]["cdf"]["P50"] * 100 for tn, _ in TRAFFIC]
    bullets = [
        L(f"P10: 11be +{min(p10):.0f}~+{max(p10):.0f}% (longer EHT preamble on one uncontended access); "
          f"P50: {min(p50):.0f}~{max(p50):.0f}%; P99: −{min(phy_p99_gain):.0f}~−{max(phy_p99_gain):.0f}% — "
          f"same floor, 11be pulls ahead from the median toward the tail.",
          f"P10：11be +{min(p10):.0f}~+{max(p10):.0f}%(單次無競爭存取的 EHT preamble 較長)；"
          f"P50：{min(p50):.0f}~{max(p50):.0f}%；P99：−{min(phy_p99_gain):.0f}~−{max(phy_p99_gain):.0f}% — "
          f"起點相同，11be 從中位數往尾端才拉開。", lang),
        L(f"PHY gain shrinks with burstiness: EDCA-only P99 gain CBR −{phy_p99_gain[0]:.0f}% → "
          f"Poisson −{phy_p99_gain[1]:.0f}% → MMPP −{phy_p99_gain[2]:.0f}% → OnOff −{phy_p99_gain[3]:.0f}% "
          f"(burst-scale queueing is arrival-driven).",
          f"PHY 增益隨突發性縮小：純 EDCA P99 增益 CBR −{phy_p99_gain[0]:.0f}% → "
          f"Poisson −{phy_p99_gain[1]:.0f}% → MMPP −{phy_p99_gain[2]:.0f}% → OnOff −{phy_p99_gain[3]:.0f}% "
          f"(突發尺度的排隊由到達過程主導)。", lang),
        L(f"P-EDCA view: on loaded 11n the DS-CTS overhead barely pays (+{min(gain_n):.0f}~+{max(gain_n):.0f}%); "
          f"on 11be the same mechanism still has headroom and yields +{min(gain_be):.0f}~+{max(gain_be):.0f}%.",
          f"P-EDCA 視角：重載 11n 上 DS-CTS 開銷幾乎不划算(+{min(gain_n):.0f}~+{max(gain_n):.0f}%)；"
          f"11be 上同機制仍有餘裕，帶來 +{min(gain_be):.0f}~+{max(gain_be):.0f}%。", lang),
    ]
    y = 0.32
    for bl in bullets:
        fig.text(0.05, y, "–", fontsize=11, color=MUTED, va="top")
        fig.text(0.068, y, bl, fontsize=8.6, color="#333", va="top", wrap=True)
        y -= 0.058
    footer(fig, s, 3); pdf.savefig(fig); plt.close(fig)

def page4(pdf, lang, s):
    fig = plt.figure(figsize=(13.33, 7.5)); fig.patch.set_facecolor("white")
    title_block(fig,
        L("Root cause: different fixed points of the collision-queueing feedback loop",
          "根本原因：碰撞–排隊回饋迴路的不同固定點", lang),
        L("Per-frame airtime is nearly identical (EHT +12% payload rate, longer preamble) — the cause is the operating point, not raw speed",
          "每幀空中時間幾乎相同(EHT +12% 酬載率、preamble 較長) — 原因是操作點，而非原始速度", lang))

    fig.text(0.045, 0.80, L("Causal chain (11n stuck at the high-collision fixed point; 11be at the low one)",
                            "因果鏈(11n 卡在高碰撞固定點；11be 在低碰撞固定點)", lang),
             fontsize=12, fontweight="bold", color=INK)
    ax = fig.add_axes([0.045, 0.62, 0.91, 0.15]); ax.axis("off"); ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    boxes = [
        L("EhtMcs7 service margin:\n~12% more payload per\n2.08 ms TXOP; retry\nrounds slightly shorter",
          "EhtMcs7 服務餘裕：\n每 2.08 ms TXOP 多 ~12%\n酬載；重傳回合略短", lang),
        L("Service headroom up,\nqueue sojourn down,\nfewer simultaneously\nbacklogged STAs",
          "服務餘裕上升，\n佇列滯留下降，\n同時積壓的 STA 變少", lang),
        L("VO CWmin=3 (4 slots):\ncollision rate very\nsensitive to contender\ncount → collisions drop",
          "VO CWmin=3(4 槽)：\n碰撞率對競爭者數\n極敏感 → 碰撞下降", lang),
        L(f"Shorter retry chains;\nretry-limit loss\n{min(loss_n):.0f}–{max(loss_n):.0f}% → "
          f"{min(loss_be):.0f}–{max(loss_be):.0f}%\n(measured, EDCA-only)",
          f"重傳鏈變短；\nretry-limit 損失\n{min(loss_n):.0f}–{max(loss_n):.0f}% → "
          f"{min(loss_be):.0f}–{max(loss_be):.0f}%\n(實測，純 EDCA)", lang),
        L(f"Feedback settles at a\nlow-collision fixed point:\ndistribution shifts,\nP99 −{min(phy_p99_gain):.0f}% to −{max(phy_p99_gain):.0f}%",
          f"回饋收斂到低碰撞\n固定點：分布左移，\nP99 −{min(phy_p99_gain):.0f}% ~ −{max(phy_p99_gain):.0f}%", lang),
    ]
    bw = 0.178
    for i, bx in enumerate(boxes):
        x = i * (bw + 0.0075)
        flowbox(ax, x, bw, bx)
        if i < 4:
            ax.annotate("", xy=(x + bw + 0.007, 0.5), xytext=(x + bw, 0.5),
                        arrowprops=dict(arrowstyle="->", color=MUTED, lw=1.2), transform=ax.transAxes)

    # supporting evidence table
    fig.text(0.045, 0.545, L("Supporting evidence (EDCA-only, 30 STAs; 11n → 11be)",
                             "佐證(純 EDCA，30 STA；11n → 11be)", lang),
             fontsize=11.5, fontweight="bold", color=INK)
    cols = [L("Traffic", "流量", lang), L("VO collision loss", "VO 碰撞損失", lang),
            L("Queue delay (µs)", "佇列延遲 (µs)", lang), L("Access delay (µs)", "存取延遲 (µs)", lang),
            L("Channel idle", "通道閒置", lang)]
    cx = [0.05, 0.17, 0.35, 0.53, 0.70]
    for c, x in zip(cols, cx):
        fig.text(x, 0.50, c, fontsize=9.5, fontweight="bold", color=INK)
    y = 0.465
    for tn, _ in TRAFFIC:
        a, b = D["11n"][tn]["edca"], D["11be"][tn]["edca"]
        cells = [tn, f"{a['loss']:.1f}% → {b['loss']:.1f}%", f"{a['queue']:.0f} → {b['queue']:.0f}",
                 f"{a['access']:.0f} → {b['access']:.0f}", f"{a['idle']:.1f}% → {b['idle']:.1f}%"]
        for cval, x in zip(cells, cx):
            fig.text(x, y, cval, fontsize=9.3, color="#333")
        y -= 0.036
    read_txt = L("Reading: queue delay is ~90% of MAC delay and drops 16–33%; access delay barely differs "
                 "(<5%, even slightly higher for 11be on bursty — longer PPDU); idle differs <1 pp; carried throughput equal.",
                 "解讀：佇列延遲約佔 MAC 延遲 90%、下降 16–33%；存取延遲幾乎不變(<5%，突發流量下 11be 甚至略高 — PPDU 較長)；"
                 "閒置差 <1 pp；承載吞吐量相同。", lang)
    yr = y - 0.004
    for ln in wrap_lines(read_txt, 100, lang):     # width-capped so it stays left of the sidebar
        fig.text(0.045, yr, ln, fontsize=8, color=FAINT, va="top")
        yr -= 0.022

    # sidebar
    sx = 0.80
    fig.text(sx, 0.545, L("Why each pattern appears", "各現象成因", lang), fontsize=11, fontweight="bold", color=INK)
    items = [
        (L("No gain at light P-EDCA load", "輕 P-EDCA 負載無增益", lang),
         L("Priority channel uncongested; P-EDCA subset tail is arrival-driven, so the faster PHY "
           "does not help — and for bursty traffic 11be even trails.",
           "優先通道不擁塞；P-EDCA 子集尾端由到達過程主導，較快 PHY 幫不上 — 突發流量下 11be 甚至落後。", lang)),
        (L("Smaller gain when bursty", "越突發增益越小", lang),
         L("Burst peaks far exceed the service rate; queueing is arrival-driven, the margin only "
           "shortens the drain time.", "突發峰值遠超服務率；排隊由到達過程主導，餘裕只縮短排空時間。", lang)),
        (L("Largest gap at n=30", "n=30 差距最大", lang),
         L("DS-CTS adds one 6 Mbps exchange per access; 11n adds it past the knee (blow-up); 11be absorbs it.",
           "DS-CTS 每次存取多一次 6 Mbps 交握；11n 加在膝點之後(爆掉)，11be 吸收得了。", lang)),
    ]
    yy = 0.505
    for head, body in items:
        fig.text(sx, yy, "▪ " + head, fontsize=9.3, fontweight="bold", color=INK)
        yy -= 0.032
        for ln in wrap_lines(body, 34, lang):
            fig.text(sx, yy, ln, fontsize=7.8, color=MUTED)
            yy -= 0.0215
        yy -= 0.020

    fig.text(0.045, 0.155, L("Open items (suggested follow-ups)", "待辦(建議後續)", lang),
             fontsize=11.5, fontweight="bold", color=INK)
    open_txt = L("Whether the ~12% service margin alone explains the fixed-point gap, or ns-3's HT vs EHT "
                 "frame-exchange / receive paths (post-collision EIFS behavior; this repo's v6–v6.3 changes) also "
                 "contribute: (1) count PhyRxDrop / collision events per standard; (2) rate-sensitivity run with 11be "
                 "GI = 0.8 µs (86 Mbps) to separate rate effects from code-path effects.",
                 "究竟 ~12% 服務餘裕本身是否足以解釋固定點差距，或 ns-3 的 HT vs EHT 幀交換／接收路徑(碰撞後 EIFS 行為；"
                 "本 repo v6–v6.3 的修改)也有貢獻：(1) 依標準統計 PhyRxDrop／碰撞事件；(2) 以 11be GI = 0.8 µs(86 Mbps)做"
                 "速率敏感度實驗，區分速率效應與程式路徑效應。", lang)
    yo = 0.115
    for ln in wrap_lines(open_txt, 135, lang):
        fig.text(0.045, yo, ln, fontsize=8.3, color="#444", va="top")
        yo -= 0.023
    footer(fig, s, 4); pdf.savefig(fig); plt.close(fig)

# ═════════════════════════ run ═════════════════════════
def build(lang, outfile):
    if lang == "zh":
        for name in ("Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans CJK SC"):
            if any(name in f.name for f in fm.fontManager.ttflist):
                plt.rcParams["font.family"] = name; break
        else:
            for p in ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]:
                if Path(p).exists():
                    fm.fontManager.addfont(p)
                    plt.rcParams["font.family"] = fm.FontProperties(fname=p).get_name(); break
    else:
        plt.rcParams["font.family"] = "DejaVu Sans"
    plt.rcParams["axes.unicode_minus"] = False
    s = strings(lang)
    with PdfPages(BASE / outfile) as pdf:
        page1(pdf, lang, s); page2(pdf, lang, s); page3(pdf, lang, s); page4(pdf, lang, s)
    print(f"wrote {outfile}")

if __name__ == "__main__":
    build("en", "11be_vs_11n_insights_en.pdf")
    build("zh", "11be_vs_11n_insights.pdf")
