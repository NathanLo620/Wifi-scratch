#!/usr/bin/env python3
"""
Regenerate the 802.11be vs 802.11n P-EDCA tail-latency insight deck (4 pages),
in both English and Traditional-Chinese, straight from the current sweep CSVs.

Data sources (per PHY in {11n,11be} × traffic in {CBR,Poisson,MMPP,OnOff}):
  <phy>/fix_nsta30_CwdsxQSRCxPSRC_sweep[_poisson|_MMPP|_onoff]/
      combo_percentile_summary_1Mbps.csv        -> min/median P99 over 36 combos
      best_vs_default_p99_gain_1Mbps.csv         -> gain vs EDCA-only / default
      edca_only/pedca_count_sweep_statistics_edca_only_1Mbps.txt  -> VO loss/queue/access/idle/thr
      edca_only/edca_only_p00_vo_delay_pdf_nSta30_1Mbps.csv       -> EDCA-only VO delay CDF

Dataset vintage (IMPORTANT):
  * 11n  sweeps ran 2026-06-22 on v6.2.1 code.
  * 11be sweeps re-ran 2026-07-18 on v6.3.2 code (EHT-compat + NAV + FEM fixes),
    with the PHY re-configured to EhtMcs5 @ GI 1600 ns = 65.0 Mbps, i.e. RATE-MATCHED
    to 11n HtMcs7 @ GI 800 ns (also 65.0 Mbps).  BA window 64 on both.
  * --dumpPhy airtime: 1x1000B PPDU EHT 177 us vs HT 160 us (+10.6%);
    per-MPDU inside a 16-MPDU A-MPDU: 126.25 vs 125.5 us (+0.6%).

Metric conventions (match the original deck):
  * Page-2 P99 bars use delay_type = 'pedca' (P-EDCA STA perspective).
  * Delta shown = (11be - 11n)/11n : negative (green) => 11be lower/better.
  * Page-3 gain bars = gain_vs_EDCA at nPedca=30, delay_type 'pedca'.

Outputs (overwrites in place):
  11be_vs_11n_insights_en.pdf
  11be_vs_11n_insights.pdf   (Traditional Chinese)
"""
import csv, re, statistics, textwrap
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
SIM_TIME = 10.0

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
    seen, rows = set(), []
    for r in csv.DictReader(open(sdir(phy, suf) / "combo_percentile_summary_1Mbps.csv")):
        k = (r["CWds"], r["QSRC"], r["PSRC"], r["nPedca"], r["delay_type"])
        if k in seen: continue                     # summaries are 2x-duplicated after resume
        seen.add(k); rows.append(r)
    out = {}
    for dt in ("pedca", "vo", "legacy"):
        for n in NPEDCAS:
            sub = [r for r in rows if r["delay_type"] == dt and int(r["nPedca"]) == n]
            if not sub: continue
            best = min(sub, key=lambda r: float(r["P99_us"]))
            out[(dt, n)] = dict(minP99=float(best["P99_us"]),
                                medP99=statistics.median(float(r["P99_us"]) for r in sub),
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
                     ("access", r"Avg Access Delay:\s*(-?[\d.]+)"), ("thr", r"Throughput:\s*([\d.]+)"),
                     ("succ", r"Successes:\s*([\d.]+)")]:
            mm = re.search(p, t); d[k] = float(mm.group(1)) if mm else 0
    # channel-busy airtime spent per successfully delivered VO MPDU (us)
    d["busy_per_mpdu"] = (100 - d["idle"]) / 100 * SIM_TIME / d["succ"] * 1e6 if d.get("succ") else 0
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

def best_delta(n):
    """(11be - 11n)/11n % on best-of-36 P-EDCA-STA P99, per traffic."""
    out = []
    for tn, _ in TRAFFIC:
        a = D["11n"][tn]["summary"][("pedca", n)]["minP99"]
        b = D["11be"][tn]["summary"][("pedca", n)]["minP99"]
        out.append((b - a) / a * 100)
    return out

def med_delta(n):
    out = []
    for tn, _ in TRAFFIC:
        a = D["11n"][tn]["summary"][("pedca", n)]["medP99"]
        b = D["11be"][tn]["summary"][("pedca", n)]["medP99"]
        out.append((b - a) / a * 100)
    return out

p99_n30 = {tn: (D["11n"][tn]["summary"][("pedca", 30)]["minP99"] / 1000,
               D["11be"][tn]["summary"][("pedca", 30)]["minP99"] / 1000) for tn, _ in TRAFFIC}
red_n30 = [(a - b) / a * 100 for a, b in p99_n30.values()]          # reduction % (positive = 11be better)
be_n30  = [b for _, b in p99_n30.values()]
n_n30   = [a for a, _ in p99_n30.values()]
d5, d15, dmed30 = best_delta(5), best_delta(15), med_delta(30)
loss_n  = [D["11n"][tn]["edca"]["loss"] for tn, _ in TRAFFIC]
loss_be = [D["11be"][tn]["edca"]["loss"] for tn, _ in TRAFFIC]
gain_be = [D["11be"][tn]["gain"][("pedca", 30)]["gain_vs_EDCA"] for tn, _ in TRAFFIC]
gain_n  = [D["11n"][tn]["gain"][("pedca", 30)]["gain_vs_EDCA"] for tn, _ in TRAFFIC]
gain_be5 = [D["11be"][tn]["gain"][("pedca", 5)]["gain_vs_EDCA"] for tn, _ in TRAFFIC]
gain_n5  = [D["11n"][tn]["gain"][("pedca", 5)]["gain_vs_EDCA"] for tn, _ in TRAFFIC]
# EDCA-only PHY P99 delta per traffic (11be vs 11n; positive = 11be worse)
phy_p99_delta = [(D["11be"][tn]["cdf"]["P99"] - D["11n"][tn]["cdf"]["P99"]) / D["11n"][tn]["cdf"]["P99"] * 100
                 for tn, _ in TRAFFIC]
phy_p99_ratio = [D["11be"][tn]["cdf"]["P99"] / D["11n"][tn]["cdf"]["P99"] for tn, _ in TRAFFIC]
edca_p99_n  = [D["11n"][tn]["cdf"]["P99"] for tn, _ in TRAFFIC]
edca_p99_be = [D["11be"][tn]["cdf"]["P99"] for tn, _ in TRAFFIC]
thr_delta = [(D["11be"][tn]["edca"]["thr"] - D["11n"][tn]["edca"]["thr"]) / D["11n"][tn]["edca"]["thr"] * 100
             for tn, _ in TRAFFIC]
busy_delta = [(D["11be"][tn]["edca"]["busy_per_mpdu"] - D["11n"][tn]["edca"]["busy_per_mpdu"])
              / D["11n"][tn]["edca"]["busy_per_mpdu"] * 100 for tn, _ in TRAFFIC]
queue_delta = [(D["11be"][tn]["edca"]["queue"] - D["11n"][tn]["edca"]["queue"])
               / D["11n"][tn]["edca"]["queue"] * 100 for tn, _ in TRAFFIC]
access_delta = [(D["11be"][tn]["edca"]["access"] - D["11n"][tn]["edca"]["access"])
                / D["11n"][tn]["edca"]["access"] * 100 for tn, _ in TRAFFIC]

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
    S["footer"] = L("ns-3.45 P-EDCA sims | scratch/delay_pdf/{11n,11be} | 11n: 2026-06-22 (v6.2.1) · 11be: 2026-07-18 (v6.3.2)",
                    "ns-3.45 P-EDCA 模擬 | scratch/delay_pdf/{11n,11be} | 11n：2026-06-22(v6.2.1)· 11be：2026-07-18(v6.3.2)", lang)
    return S

# ═════════════════════════ drawing helpers ═════════════════════════
def footer(fig, s, page):
    fig.text(0.045, 0.035, s["footer"], fontsize=7.5, color=FAINT, va="center")
    fig.text(0.955, 0.035, f"{page} / 4", fontsize=7.5, color=FAINT, va="center", ha="right")

def card(fig, x, y, w, h, big, sub, small, big_color=INK, big_fs=25):
    ax = fig.add_axes([x, y, w, h]); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.02, 0.04), 0.96, 0.92, boxstyle="round,pad=0.01,rounding_size=0.04",
                 fc=CARDBG, ec=CARDBD, lw=1, transform=ax.transAxes))
    ax.text(0.5, 0.70, big, ha="center", va="center", fontsize=big_fs, fontweight="bold",
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
    ttl = L("802.11be vs 802.11n: Rate-Matched P-EDCA Tail-Latency Comparison",
            "802.11be vs 802.11n：速率對齊下的 P-EDCA 尾延遲比較", lang)
    sub = L("CWds × QSRC × PSRC parameter sweep | CBR / Poisson / MMPP / OnOff traffic | P-EDCA STA perspective",
            "CWds × QSRC × PSRC 參數掃描 | CBR / Poisson / MMPP / OnOff 四種流量 | P-EDCA STA 視角", lang)
    title_block(fig, ttl, sub)

    fig.text(0.045, 0.80, L("Simulation setup", "模擬設定", lang), fontsize=14, fontweight="bold", color=INK)
    rows = [
        (L("Topology", "拓樸", lang), L("30 STAs → 1 AP, random 1–5 m disc; 5 GHz ch36 / 20 MHz",
                                       "30 STAs → 1 AP，隨機分布於 1–5 m；5 GHz ch36 / 20 MHz", lang)),
        (L("Traffic", "流量", lang), L("Uplink AC_VO UDP, 1 Mbps per STA (1000 B); CBR / Poisson / MMPP / OnOff",
                                      "上行 AC_VO UDP，每 STA 1 Mbps(1000 B)；CBR / Poisson / MMPP / OnOff", lang)),
        (L("PHY", "PHY", lang), L("11n = HtMcs7 @ 0.8 µs GI  vs  11be = EhtMcs5 @ 1.6 µs GI — both 65.0 Mbps (rate-matched)",
                                  "11n = HtMcs7 @ 0.8 µs GI vs 11be = EhtMcs5 @ 1.6 µs GI — 同為 65.0 Mbps(速率對齊)", lang)),
        (L("Airtime", "空中時間", lang), L("1×1000 B PPDU: EHT 177 µs vs HT 160 µs (+10.6%); per-MPDU @16× A-MPDU +0.6%",
                                          "單 MPDU PPDU：EHT 177 µs vs HT 160 µs(+10.6%)；16 條聚合時 per-MPDU 僅 +0.6%", lang)),
        (L("Control", "控制幀", lang), L("OFDM 6 Mbps for both; A-MPDU on, BA window 64 on both; VO TXOP limit 2.08 ms",
                                        "兩者同為 OFDM 6 Mbps；A-MPDU 開啟、BA window 皆 64；VO TXOP 上限 2.08 ms", lang)),
        (L("Sweep", "掃描", lang), "nPedca ∈ {5, 15, 30}; CWds{0,1} × QSRC{0–5} × PSRC{1–3} = 36 combos"),
        (L("Statistics", "統計", lang), L("10 seeds × 10 s per point; plus an EDCA-only baseline (nPedca = 0)",
                                         "每點 10 seeds × 10 s；另含純 EDCA 基準(nPedca = 0)", lang)),
    ]
    y = 0.745
    for k, v in rows:
        fig.text(0.055, y, k, fontsize=10, fontweight="bold", color=INK, va="center")
        fig.text(0.16, y, v, fontsize=10, color="#333", va="center")
        y -= 0.048
    cav = L("Caveat: 11n data ran 2026-06-22 on v6.2.1 code; 11be re-ran 2026-07-18 on v6.3.2 (EHT NAV / "
            "frame-exchange fixes) — cross-standard deltas mix PHY and code-vintage effects.",
            "注意：11n 資料為 2026-06-22 以 v6.2.1 程式所跑；11be 於 2026-07-18 以 v6.3.2(EHT NAV／幀交換修正)重跑 — "
            "跨標準差異混合了 PHY 與程式版本效應。", lang)
    yc = y - 0.005
    for ln in wrap_lines(cav, 95, lang):        # width-capped so it stays clear of the cards
        fig.text(0.045, yc, ln, fontsize=8, color=FAINT, va="center")
        yc -= 0.022

    # cards (recomputed)
    c1_lo, c1_hi = rng(red_n30)
    r_lo, r_hi = min(phy_p99_ratio), max(phy_p99_ratio)
    card(fig, 0.60, 0.66, 0.35, 0.155,
         f"P99  −{c1_hi}% ~ −{c1_lo}%",
         L("Full P-EDCA penetration (30/30), best params: 11be wins",
           "全滲透(30/30 P-EDCA)最佳參數下：11be 勝出", lang),
         L(f"11be {min(be_n30):.1f}–{max(be_n30):.1f} ms vs 11n {min(n_n30):.0f}–{max(n_n30):.0f} ms — only at n=30 (see p.2)",
           f"11be {min(be_n30):.1f}–{max(be_n30):.1f} ms vs 11n {min(n_n30):.0f}–{max(n_n30):.0f} ms — 僅限 n=30(見第 2 頁)", lang),
         big_color=GREEN)
    card(fig, 0.60, 0.475, 0.35, 0.155,
         f"EDCA-only  ×{r_lo:.1f}–{r_hi:.1f}",
         L("Without P-EDCA the rate-matched 11be tail is WORSE",
           "無 P-EDCA 時，速率對齊的 11be 尾延遲反而更差", lang),
         L(f"P99 {min(edca_p99_n):.0f}–{max(edca_p99_n):.0f} → {min(edca_p99_be):.0f}–{max(edca_p99_be):.0f} ms; "
           f"carried VO throughput {min(thr_delta):.0f}% ~ {max(thr_delta):.0f}%",
           f"P99 {min(edca_p99_n):.0f}–{max(edca_p99_n):.0f} → {min(edca_p99_be):.0f}–{max(edca_p99_be):.0f} ms；"
           f"VO 承載吞吐 {min(thr_delta):.0f}% ~ {max(thr_delta):.0f}%", lang),
         big_color=RED)
    card(fig, 0.60, 0.29, 0.35, 0.155,
         f"+{min(gain_be):.0f}% ~ +{max(gain_be):.0f}% vs EDCA",
         L("P-EDCA value at n=30 is huge on 11be, tiny on 11n", "n=30 時 P-EDCA 在 11be 上增益極大、11n 上極小", lang),
         L(f"11n only +{min(gain_n):.0f}% ~ +{max(gain_n):.0f}% — the penetration trends run in OPPOSITE directions",
           f"11n 僅 +{min(gain_n):.0f}% ~ +{max(gain_n):.0f}% — 兩者的滲透率趨勢方向相反", lang))

    # takeaway
    ax = fig.add_axes([0.045, 0.10, 0.52, 0.16]); ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.0, 0.0), 1, 1, boxstyle="round,pad=0.01,rounding_size=0.03",
                 fc="#FAFAF9", ec=CARDBD, lw=1, transform=ax.transAxes))
    ax.text(0.04, 0.80, L("One-line takeaway", "一句話結論", lang), fontsize=11, fontweight="bold",
            color=INK, transform=ax.transAxes)
    ax.text(0.04, 0.42, L("At the SAME 65 Mbps PHY rate, 11be's fixed per-PPDU overhead makes its saturated EDCA\n"
                          "baseline ~2× worse at P99; aggressive P-EDCA (QSRC0/PSRC3) reclaims that dead time and\n"
                          "pushes fully-penetrated 11be 22–46% below the best 11n — but partial penetration still trails.",
                          "在相同 65 Mbps PHY 速率下，11be 每 PPDU 的固定開銷使其飽和 EDCA 基準\n"
                          "P99 差約 2 倍；積極 P-EDCA(QSRC0/PSRC3)把這些死時間收回來，\n"
                          "讓全滲透的 11be 比最佳 11n 低 22–46% — 但部分滲透時 11be 仍落後。", lang),
            fontsize=9.5, color="#333", va="center", transform=ax.transAxes, linespacing=1.5)
    footer(fig, s, 1); pdf.savefig(fig); plt.close(fig)

def page2(pdf, lang, s):
    fig = plt.figure(figsize=(13.33, 7.5)); fig.patch.set_facecolor("white")
    title_block(fig,
        L("Core result: P-EDCA P99 latency — 11be only wins at FULL penetration",
          "核心結果：P-EDCA P99 延遲 — 11be 只在「全滲透」時勝出", lang),
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
        L(f"Light penetration n=5: 11be is far WORSE (+{min(d5):.0f}% to +{max(d5):.0f}%). 11n's small P-EDCA subset "
          f"rides a healthy baseline and reaches {min(D['11n'][tn]['summary'][('pedca',5)]['minP99'] for tn,_ in TRAFFIC)/1000:.1f}–"
          f"{max(D['11n'][tn]['summary'][('pedca',5)]['minP99'] for tn,_ in TRAFFIC)/1000:.1f} ms; 11be's subset "
          f"is dragged by its congested surroundings (23–31 ms).",
          f"低滲透 n=5：11be 明顯較差(+{min(d5):.0f}% ~ +{max(d5):.0f}%)。11n 的少數 P-EDCA STA 騎在健康的基準上，"
          f"可達 {min(D['11n'][tn]['summary'][('pedca',5)]['minP99'] for tn,_ in TRAFFIC)/1000:.1f}–"
          f"{max(D['11n'][tn]['summary'][('pedca',5)]['minP99'] for tn,_ in TRAFFIC)/1000:.1f} ms；"
          f"11be 的子集被壅塞的環境拖累(23–31 ms)。", lang),
        L(f"Medium n=15: still +{min(d15):.0f}% to +{max(d15):.0f}%.  Full penetration n=30: the sign flips — "
          f"11be −{min(red_n30):.0f}% to −{max(red_n30):.0f}% "
          f"(11n {min(n_n30):.0f}–{max(n_n30):.0f} ms vs 11be {min(be_n30):.1f}–{max(be_n30):.1f} ms).",
          f"中滲透 n=15：仍 +{min(d15):.0f}% ~ +{max(d15):.0f}%。全滲透 n=30：符號翻轉 — "
          f"11be −{min(red_n30):.0f}% ~ −{max(red_n30):.0f}% "
          f"(11n {min(n_n30):.0f}–{max(n_n30):.0f} ms vs 11be {min(be_n30):.1f}–{max(be_n30):.1f} ms)。", lang),
        L(f"The n=30 win is parameter-dependent, not free: the MEDIAN over all 36 combos is still "
          f"+{min(dmed30):.0f}% to +{max(dmed30):.0f}% (11be worse); only aggressive QSRC 0–1 + PSRC 3 flips the sign. "
          f"All four 11be winners are c*q0s3.",
          f"n=30 的勝出依賴參數、並非白吃：36 組合的中位數仍 +{min(dmed30):.0f}% ~ +{max(dmed30):.0f}%(11be 較差)；"
          f"只有積極的 QSRC 0–1 + PSRC 3 能翻轉符號。11be 四個流量的最佳組合都是 c*q0s3。", lang),
        L(f"Gain-vs-own-EDCA runs in OPPOSITE directions with penetration: 11n +{min(gain_n5):.0f}~+{max(gain_n5):.0f}% "
          f"at n=5 collapsing to +{min(gain_n):.0f}~+{max(gain_n):.0f}% at n=30 (priority dilutes); 11be "
          f"+{min(gain_be5):.0f}~+{max(gain_be5):.0f}% rising to +{min(gain_be):.0f}~+{max(gain_be):.0f}%.",
          f"「相對自身 EDCA 的增益」隨滲透率的走向相反：11n 從 n=5 的 +{min(gain_n5):.0f}~+{max(gain_n5):.0f}% "
          f"崩落到 n=30 的 +{min(gain_n):.0f}~+{max(gain_n):.0f}%(優先權被稀釋)；11be 則從 "
          f"+{min(gain_be5):.0f}~+{max(gain_be5):.0f}% 升到 +{min(gain_be):.0f}~+{max(gain_be):.0f}%。", lang),
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
        L("Mechanism: per-PPDU overhead amplified by saturation queueing",
          "機制：每 PPDU 固定開銷被飽和排隊放大", lang),
        L("EDCA-only baseline (no P-EDCA, 30 STAs) isolates the two PHYs at the same 65 Mbps — 11be's whole distribution shifts right.",
          "純 EDCA 基準(無 P-EDCA，30 STA)在同為 65 Mbps 下隔離兩個 PHY — 11be 的整個延遲分布右移。", lang))

    for i, tn in enumerate(["CBR", "OnOff"]):
        ax = fig.add_axes([0.06 + i * 0.24, 0.42, 0.20, 0.36])
        for phy, col in [("11n", ORANGE), ("11be", BLUE)]:
            c = D[phy][tn]["cdf"]; ax.plot(c["mids"], c["cdf"], color=col, lw=1.8, label=f"802.{phy}")
        ax.set_xlim(0, 55); ax.set_ylim(0, 1.02)
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
        L(f"The right-shift grows toward the tail: P10 +{min(p10):.0f}~+{max(p10):.0f}% (≈ the longer EHT preamble), "
          f"P50 +{min(p50):.0f}~+{max(p50):.0f}%, P99 +{min(phy_p99_delta):.0f}~+{max(phy_p99_delta):.0f}% — "
          f"the signature of a queueing system pushed deeper into saturation, not of slower frames.",
          f"右移幅度往尾端遞增：P10 +{min(p10):.0f}~+{max(p10):.0f}%(≈較長的 EHT preamble)、"
          f"P50 +{min(p50):.0f}~+{max(p50):.0f}%、P99 +{min(phy_p99_delta):.0f}~+{max(phy_p99_delta):.0f}% — "
          f"這是排隊系統被推得更深入飽和的特徵，而非「每幀變慢」。", lang),
        L(f"The deficit widens with burstiness: carried VO throughput {thr_delta[0]:.0f}% (CBR) → {thr_delta[1]:.0f}% "
          f"(Poisson) → {thr_delta[2]:.0f}% (MMPP) → {thr_delta[3]:.0f}% (OnOff); busy airtime per delivered MPDU "
          f"+{min(busy_delta):.0f}% → +{max(busy_delta):.0f}% — burstier arrivals mean more small PPDUs, "
          f"each paying the fixed EHT preamble tax.",
          f"劣勢隨突發性擴大：VO 承載吞吐 {thr_delta[0]:.0f}%(CBR) → {thr_delta[1]:.0f}%(Poisson) → "
          f"{thr_delta[2]:.0f}%(MMPP) → {thr_delta[3]:.0f}%(OnOff)；每成功送達 MPDU 的忙碌空時 "
          f"+{min(busy_delta):.0f}% ~ +{max(busy_delta):.0f}% — 越突發、小 PPDU 越多，每個都付一次固定 preamble 稅。", lang),
        L(f"P-EDCA view: at n=30 the DS-CTS mechanism barely pays on 11n (+{min(gain_n):.0f}~+{max(gain_n):.0f}%) "
          f"but recovers 11be's dead time (+{min(gain_be):.0f}~+{max(gain_be):.0f}%), landing 11be at "
          f"{min(be_n30):.1f}–{max(be_n30):.1f} ms — below 11n's best.",
          f"P-EDCA 視角：n=30 時 DS-CTS 機制在 11n 上幾乎不划算(+{min(gain_n):.0f}~+{max(gain_n):.0f}%)，"
          f"在 11be 上卻收回大量死時間(+{min(gain_be):.0f}~+{max(gain_be):.0f}%)，"
          f"使 11be 落在 {min(be_n30):.1f}–{max(be_n30):.1f} ms — 低於 11n 的最佳值。", lang),
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
        L("Root cause: a fixed per-access tax amplified by queueing",
          "根本原因：每次存取的固定稅被排隊放大", lang),
        L("Rate-matched at 65 Mbps — the difference is per-PPDU overhead and operating point, not per-bit speed",
          "速率對齊在 65 Mbps — 差異來自每 PPDU 開銷與操作點，而非每位元速度", lang))

    fig.text(0.045, 0.80, L("Causal chain (EDCA-only, 30 saturated VO STAs; measured values)",
                            "因果鏈(純 EDCA，30 個飽和 VO STA；皆為實測值)", lang),
             fontsize=12, fontweight="bold", color=INK)
    ax = fig.add_axes([0.045, 0.62, 0.91, 0.15]); ax.axis("off"); ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    boxes = [
        L("Same 65 Mbps rate, but\nEHT pays +17 µs/PPDU\n(preamble + 14.4 µs\nsymbol padding): +10.6%\non a 1-MPDU PPDU",
          "同為 65 Mbps，但 EHT\n每 PPDU 多付 +17 µs\n(preamble + 14.4 µs\n符號填補)：單 MPDU\nPPDU +10.6%", lang),
        L(f"Saturation = many small\nPPDUs → busy airtime per\ndelivered MPDU\n+{min(busy_delta):.0f}% ~ +{max(busy_delta):.0f}%",
          f"飽和下多為小 PPDU →\n每成功 MPDU 的忙碌\n空時 +{min(busy_delta):.0f}% ~ +{max(busy_delta):.0f}%", lang),
        L(f"Service efficiency drops:\ncarried VO throughput\n{min(thr_delta):.0f}% ~ {max(thr_delta):.0f}%\nat identical offered load",
          f"服務效率下降：\n同樣供給負載下\nVO 承載吞吐\n{min(thr_delta):.0f}% ~ {max(thr_delta):.0f}%", lang),
        L(f"Queues sit deeper:\navg queue delay\n+{min(queue_delta):.0f}% ~ +{max(queue_delta):.0f}%\n(loss ≈ same → NOT\nmore collisions)",
          f"佇列更深：\n平均佇列延遲\n+{min(queue_delta):.0f}% ~ +{max(queue_delta):.0f}%\n(損失率近似 → 並非\n碰撞變多)", lang),
        L(f"Queueing amplifies\ntoward the tail:\nP50 +12~23%,\nP99 ×{min(phy_p99_ratio):.1f}–×{max(phy_p99_ratio):.1f}",
          f"排隊往尾端放大：\nP50 +12~23%，\nP99 ×{min(phy_p99_ratio):.1f}–×{max(phy_p99_ratio):.1f}", lang),
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
    cols = [L("Traffic", "流量", lang), L("VO retry-limit loss", "VO 重傳上限損失", lang),
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
    read_txt = L(f"Reading: queue delay dominates MAC delay and rises {min(queue_delta):.0f}–{max(queue_delta):.0f}%; "
                 f"access delay rises {min(access_delta):.0f}–{max(access_delta):.0f}% (longer PPDU + response timing); "
                 f"idle even rises 1.4–2.0 pp — the channel is not busier, it is less efficient per access; "
                 f"retry-limit loss is slightly LOWER on 11be, ruling out a collision-rate explanation.",
                 f"解讀：佇列延遲主導 MAC 延遲、上升 {min(queue_delta):.0f}–{max(queue_delta):.0f}%；"
                 f"存取延遲上升 {min(access_delta):.0f}–{max(access_delta):.0f}%(PPDU 較長＋回應時序)；"
                 f"閒置甚至上升 1.4–2.0 pp — 通道並沒有更忙，而是每次存取的效率更差；"
                 f"11be 的重傳上限損失還略低，排除「碰撞變多」的解釋。", lang)
    yr = y - 0.004
    for ln in wrap_lines(read_txt, 100, lang):     # width-capped so it stays left of the sidebar
        fig.text(0.045, yr, ln, fontsize=8, color=FAINT, va="top")
        yr -= 0.022

    # sidebar
    sx = 0.80
    fig.text(sx, 0.545, L("Why each pattern appears", "各現象成因", lang), fontsize=11, fontweight="bold", color=INK)
    items = [
        (L("P-EDCA flips the ranking at n=30", "n=30 時 P-EDCA 翻轉排名", lang),
         L("DS-CTS ordering replaces CWmin=3 collision contention — exactly the dead time 11be "
           "loses; 11n's healthier baseline leaves little to reclaim.",
           "DS-CTS 排序取代 CWmin=3 的碰撞競爭 — 正是 11be 損失的死時間；11n 基準較健康、可回收的少。", lang)),
        (L("Partial penetration favors 11n", "部分滲透時 11n 佔優", lang),
         L("The P-EDCA subset rides on the surrounding system: 11be's congested baseline drags "
           "its subset tail even with priority access.",
           "P-EDCA 子集騎在整體系統上：11be 壅塞的基準拖累子集尾端，即使有優先存取。", lang)),
        (L("Burstier traffic widens the gap", "越突發差距越大", lang),
         L("Bursts drain in small PPDUs; each PPDU pays the fixed EHT preamble tax, so the "
           "efficiency deficit grows (−12% CBR → −18% OnOff).",
           "突發以小 PPDU 排空；每個 PPDU 都付固定 preamble 稅，效率劣勢隨之擴大(CBR −12% → OnOff −18%)。", lang)),
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
    open_txt = L("(1) Re-run the 11n sweep on current v6.3.2 code — the 11n data predates the v6.3.x P-EDCA/NAV fixes. "
                 "(2) Decompose the per-PPDU tax vs EHT code-path effects: count PhyRxDrop / collisions per standard; "
                 "re-run 11be at GI 0.8 µs (68.8 Mbps). (3) Log the A-MPDU size distribution to confirm the "
                 "small-PPDU hypothesis behind the +10~20% busy-time per MPDU.",
                 "(1) 用現行 v6.3.2 程式重跑 11n 掃描 — 11n 資料早於 v6.3.x 的 P-EDCA/NAV 修正。"
                 "(2) 拆解每 PPDU 固定稅與 EHT 程式路徑效應：依標準統計 PhyRxDrop／碰撞事件；以 GI 0.8 µs(68.8 Mbps)重跑 11be。"
                 "(3) 記錄 A-MPDU 大小分布，驗證「小 PPDU」假說是否足以解釋 +10~20% 的每 MPDU 忙碌空時。", lang)
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
