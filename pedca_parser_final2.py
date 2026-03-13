import sys

pedca_success = 0
edca_success = 0
total_dscts = 0
legacy_nav_sets = 0
rts_blocked_by_nav = 0
rts_scheduled = 0

print("Parsing pedca logs...")
with open("pedca_analysis_details2.txt", "r") as f:
    for line in f:
        if "[P-EDCA VO SUCCESS]" in line and "(Stage2)" in line:
            pedca_success += 1
        elif "[P-EDCA VO SUCCESS]" in line and "(Stage2)" not in line:
            edca_success += 1
        if "[DS-CTS NAV-SET]" in line:
            legacy_nav_sets += 1
        if "[P-EDCA STAGE1]" in line:
            total_dscts += 1
        if "[RTS-RX]" in line:
            if "Medium BUSY (NAV=" in line:
                rts_blocked_by_nav += 1
            elif "Medium IDLE -> schedule CTS" in line:
                rts_scheduled += 1

print(f"P-EDCA Successes (Stage 2): {pedca_success}")
print(f"EDCA Successes (Fallback/Direct): {edca_success}")
print(f"Total DS-CTS Sent: {total_dscts}")
print(f"Legacy NAV Set count (by DS-CTS): {legacy_nav_sets}")
print(f"RTS Blocked Due to NAV Busy at AP/STA: {rts_blocked_by_nav}")
print(f"RTS Granted CTS: {rts_scheduled}")
