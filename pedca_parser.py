import sys
import re

pedca_success = 0
edca_success = 0
total_dscts = 0
nav_sets = 0
rts_cts_collisions = 0
legacy_nav_sets = 0

print("Parsing pedca logs...")
for line in open("pedca_analysis_output.log", "r"):
    if "[P-EDCA VO SUCCESS]" in line and "(Stage2)" in line:
        pedca_success += 1
    elif "[P-EDCA VO SUCCESS]" in line and "(Stage2)" not in line:
        # Actually in script, P-EDCA stage validation falls back to EDCA
        # If gap > 97us, it falls back to normal EDCA.
        edca_success += 1
    if "[DS-CTS NAV-SET]" in line:
        legacy_nav_sets += 1
    if "[P-EDCA STAGE1]" in line:
        total_dscts += 1

print(f"P-EDCA Successes: {pedca_success}")
print(f"EDCA Successes (Fallback/Direct): {edca_success}")
print(f"Total DS-CTS Sent: {total_dscts}")
print(f"Legacy NAV Set count: {legacy_nav_sets}")
