import sys

pedca_success = 0
edca_success = 0
total_dscts = 0
nav_sets = 0
legacy_nav_sets = 0
rts_cts_collisions = 0
cbrLegNavSet = 0

print("Parsing pedca logs...")
with open("pedca_analysis_details.txt", "r") as f:
    for line in f:
        if "[P-EDCA VO SUCCESS]" in line and "(Stage2)" in line:
            pedca_success += 1
        elif "[P-EDCA VO SUCCESS]" in line and "(Stage2)" not in line:
            edca_success += 1
        if "[DS-CTS NAV-SET]" in line:
            legacy_nav_sets += 1
        if "[P-EDCA STAGE1]" in line:
            total_dscts += 1
        if "Received RTS from" in line and "schedule CTS" not in line:
            pass # We could determine RTS fail
        if "NAV" in line and "Updated NAV" in line:
            nav_sets += 1

print(f"P-EDCA Successes: {pedca_success}")
print(f"EDCA Successes (Fallback/Direct): {edca_success}")
print(f"Total DS-CTS Sent: {total_dscts}")
print(f"Legacy NAV Set count (by DS-CTS): {legacy_nav_sets}")
print(f"Total NAV Set count: {nav_sets}")
