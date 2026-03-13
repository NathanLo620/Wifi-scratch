import sys

pedca_stage2_success = 0
pedca_sta_edca_success = 0

print("Parsing pedca logs...")
with open("pedca_stage2_stats.log", "r") as f:
    for line in f:
        if "[P-EDCA VO SUCCESS]" in line:
            if "(Stage2)" in line:
                pedca_stage2_success += 1
            elif "(EDCA)" in line:
                pedca_sta_edca_success += 1

print(f"P-EDCA STA used P-EDCA Method Success (Stage 2): {pedca_stage2_success}")
print(f"P-EDCA STA used EDCA Method Success (Fallback or before QSRC trigger): {pedca_sta_edca_success}")
