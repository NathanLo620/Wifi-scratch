#!/usr/bin/env python3
"""Aggregate vs per-group VO throughput: does dual-DS add capacity or redistribute it?"""
import csv, re
from pathlib import Path

ROOT = Path("/home/nathanlo/ns-3.45/scratch/delay_pdf/11be")
MODELS = [
    ("cbr1M",     "fix_nsta30_CwdsxQSRCxPSRC_sweep",           "1Mbps"),
    ("lightload", "fix_nsta30_CwdsxQSRCxPSRC_sweep_lightload", "0.5Mbps"),
    ("MMPP",      "fix_nsta30_CwdsxQSRCxPSRC_sweep_MMPP",      "1Mbps"),
    ("onoff",     "fix_nsta30_CwdsxQSRCxPSRC_sweep_onoff",     "1Mbps"),
    ("poisson",   "fix_nsta30_CwdsxQSRCxPSRC_sweep_poisson",   "1Mbps"),
]
MODES = ["mono-DS", "dual-DS"]
NPEDCAS = [5, 15, 30]
N_STA = 30
DEFAULT = (0, 2, 1)


def tag(c, q, s):
    return f"c{c:d}_q{q:02d}_s{s:02d}"


def parse(path):
    out, cur, in_ac = {}, None, None
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        s = line.strip()
        m = re.match(r"P-EDCA STAs\s*=\s*(\d+)/\d+", s)
        if m:
            cur = int(m.group(1)); out[cur] = {}; in_ac = None; continue
        if cur is None:
            continue
        d = out[cur]
        if s.startswith("AC_") and s.endswith(":"):
            in_ac = s.rstrip(":"); continue
        if s.startswith("---") or s.startswith("==="):
            in_ac = None
        if in_ac == "AC_VO":
            if s.startswith("Throughput:"):
                mm = re.search(r"([\d.eE+-]+)\s*Mbps", s)
                if mm: d["thpt"] = float(mm.group(1))
            elif s.startswith("Successes:"):
                d["vo_succ"] = float(s.split(":")[1].strip())
            elif s.startswith("Packet Loss:"):
                mm = re.search(r"([\d.]+)\s*%", s)
                if mm: d["vo_loss"] = float(mm.group(1))
        for k, nm in [("PEDCA_STA_SUCC_COUNT", "p_succ"),
                      ("LEGACY_STA_SUCC_COUNT", "l_succ"),
                      ("PEDCA_STA_FAIL_COUNT", "p_fail"),
                      ("LEGACY_STA_FAIL_COUNT", "l_fail")]:
            if s.startswith(k + ":"):
                try: d[nm] = float(s.split(":")[1].strip())
                except ValueError: pass
                break
    return out


def pctls(d, rate, mode):
    p = d / mode / f"combo_percentile_summary_{rate}.csv"
    out = {}
    if p.exists():
        for r in csv.DictReader(p.open()):
            out[(int(r["CWds"]), int(r["QSRC"]), int(r["PSRC"]),
                 int(r["nPedca"]), r["delay_type"])] = float(r["P99_us"])
    return out


print(f"{'model':<10} {'nP':>3} {'config':<16} {'VOthpt':>8} {'vs base':>8} "
      f"{'pedcaSucc':>10} {'legSucc':>9} {'perSTA_P':>9} {'perSTA_L':>9} {'ratio':>6}")
print("-" * 100)

for key, dirname, rate in MODELS:
    D = ROOT / dirname
    base = parse(D / "mono-DS" / "edca_only" /
                 f"pedca_count_sweep_statistics_edca_only_{rate}.txt").get(0, {})
    bt = base.get("thpt", float("nan"))
    b_per = base.get("l_succ", 0) / N_STA
    print(f"{key:<10} {'-':>3} {'EDCA-only':<16} {bt:>8.2f} {'--':>8} "
          f"{0:>10.0f} {base.get('l_succ',0):>9.0f} {'--':>9} {b_per:>9.0f} {'--':>6}")
    for n in NPEDCAS:
        for mode in MODES:
            P = pctls(D, rate, mode)
            # best = min P99 for this nPedca
            best, bestv = None, None
            for c in (0, 1):
                for q in range(6):
                    for s in (1, 2, 3):
                        v = P.get((c, q, s, n, "pedca"))
                        if v is not None and (bestv is None or v < bestv):
                            bestv, best = v, (c, q, s)
            for lbl, combo in [("default", DEFAULT), ("best", best)]:
                if combo is None:
                    continue
                st = parse(D / mode / tag(*combo) /
                           f"pedca_count_sweep_statistics_{tag(*combo)}_{rate}.txt").get(n, {})
                t = st.get("thpt", float("nan"))
                ps, ls = st.get("p_succ", 0), st.get("l_succ", 0)
                per_p = ps / n if n else float("nan")
                per_l = ls / (N_STA - n) if (N_STA - n) else float("nan")
                ratio = per_p / per_l if per_l else float("nan")
                print(f"{key:<10} {n:>3} {mode[:4]+' '+lbl:<16} {t:>8.2f} "
                      f"{(t-bt)/bt*100:>+7.1f}% {ps:>10.0f} {ls:>9.0f} "
                      f"{per_p:>9.0f} {per_l:>9.0f} "
                      + (f"{ratio:>6.2f}" if ratio == ratio else f"{'--':>6}"))
    print()

# ── derived: capacity redistribution vs aggregate change ──
print("\n" + "=" * 100)
print("  DUAL-DS BEST — capacity redistribution vs aggregate throughput")
print("=" * 100)
print(f"{'model':<10} {'nP':>3} {'P-EDCA pkts/STA':>18} {'legacy pkts/STA':>19} "
      f"{'aggregate VO thpt':>22}")
print("-" * 78)
for key, dirname, rate in MODELS:
    D = ROOT / dirname
    base = parse(D / "mono-DS" / "edca_only" /
                 f"pedca_count_sweep_statistics_edca_only_{rate}.txt").get(0, {})
    b_per, bt = base.get("l_succ", 0) / N_STA, base.get("thpt", float("nan"))
    for n in NPEDCAS:
        P = pctls(D, rate, "dual-DS")
        best, bestv = None, None
        for c in (0, 1):
            for q in range(6):
                for s in (1, 2, 3):
                    v = P.get((c, q, s, n, "pedca"))
                    if v is not None and (bestv is None or v < bestv):
                        bestv, best = v, (c, q, s)
        st = parse(D / "dual-DS" / tag(*best) /
                   f"pedca_count_sweep_statistics_{tag(*best)}_{rate}.txt").get(n, {})
        pp = st.get("p_succ", 0) / n
        lp = st.get("l_succ", 0) / (N_STA - n) if N_STA - n else None
        t = st.get("thpt", float("nan"))
        legs = (f"{lp:7.0f} ({(lp-b_per)/b_per*100:+6.1f}%)" if lp else f"{'n/a (no legacy STA)':>19}")
        print(f"{key:<10} {n:>3} {pp:8.0f} ({(pp-b_per)/b_per*100:+6.1f}%) {legs:>19} "
              f"     {t:6.2f} Mbps ({(t-bt)/bt*100:+5.1f}%)")
    print(f"{'':<10} {'--':>3} {'EDCA-only reference:':>18} {b_per:6.0f} pkts/STA "
          f"        {bt:6.2f} Mbps\n")
