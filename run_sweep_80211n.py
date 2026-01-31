import subprocess
import re
import sys
import os
import csv
import collections

# Configuration
n_stas = list(range(2, 41, 2)) # 2 to 40
data_rates = ["0.1Mbps"]
sim_sec = 10
ns3_path = os.path.abspath("../ns3")
output_csv = "EDCA_Sweep_Sta_0.1Mbps.csv"

def parse_val(pattern, text, default=0.0):
    m = re.search(pattern, text)
    if m:
        return float(m.group(1))
    return default

def parse_output(output):
    """
    Parses the full simulation output and returns a nested dictionary:
    stats[ac][metric] = value
    
    NOTE: MAC_Access_Delay = MAC enqueue -> successful ACK (standard EDCA metric)
    """
    stats = collections.defaultdict(lambda: {
        "MAC_Throughput_Mbps": 0.0,
        "Loss_Pct": 0.0,
        "Retransmissions": 0.0,
        "MAC_Access_Delay_us": 0.0,  # Renamed from E2E_Delay_us
        "Queue_Delay_us": 0.0,
        "Access_Delay_us": 0.0,
    })

    # Parse Helper Stats Block (=== WifiTxStatsHelper ===)
    helper_ac_blocks = re.findall(r"(AC_[A-Z]{2}):\n\s+Successes:(.*?)(?=AC_|---|$)", output, re.DOTALL)
    for ac_label, block in helper_ac_blocks:
        ac = ac_label.replace("AC_", "")
        stats[ac]["MAC_Throughput_Mbps"] = parse_val(r"Throughput:\s+([\d\.]+) Mbps", block)
        stats[ac]["Loss_Pct"] = parse_val(r"Packet Loss:\s+([\d\.]+) %", block)
        stats[ac]["Retransmissions"] = parse_val(r"Avg Retx/MPDU:\s+([\d\.]+)", block)
        stats[ac]["Queue_Delay_us"] = parse_val(r"Avg Queue Delay:\s+([\d\.]+) us", block)
        stats[ac]["Access_Delay_us"] = parse_val(r"Avg Access Delay:\s+([\d\.]+) us", block)
        # MAC Access Delay (Enqueue -> ACK) - standard EDCA metric
        stats[ac]["MAC_Access_Delay_us"] = parse_val(r"Avg MAC Delay:\s+([\d\.]+) us", block)

    return stats

# Main Sweep
print(f"Starting Sweep. Results will be saved to {output_csv}")
headers = ["nSta", "DataRate", "AC", "MAC_Throughput_Mbps", "Loss_Pct", "Retransmissions", "MAC_Access_Delay_us", "Queue_Delay_us", "Access_Delay_us"]
print(", ".join(headers))

with open(output_csv, 'w', newline='') as csvfile:
    writer = csv.writer(csvfile)
    writer.writerow(headers)

    for n in n_stas:
        for rate in data_rates:
            print(f"Running nSta={n}, Rate={rate} (Averaging 10 Runs)...")
            
            # stats_agg[ac][metric] = [val1, val2, ...]
            stats_agg = collections.defaultdict(lambda: collections.defaultdict(list))
            
            for run_idx in range(1, 10): # Run 1 to 5
                cmd = [ns3_path, "run", f"wifi_backoff80211n --nSta={n} --dataRate={rate} --sim={sim_sec} --RngRun={run_idx}"]
                
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd(), check=True)
                    output = proc.stdout
                    
                    run_stats = parse_output(output)
                    
                    for ac in ["BE", "BK", "VI", "VO"]:
                        # Append numeric metrics
                        for k in ["MAC_Throughput_Mbps", "Loss_Pct", "Retransmissions", "MAC_Access_Delay_us", "Queue_Delay_us", "Access_Delay_us"]:
                            val = run_stats[ac].get(k, 0.0)
                            stats_agg[ac][k].append(val)

                except Exception as e:
                    print(f"Error running n={n}, rate={rate}, run={run_idx}: {e}")

            # Compute Average and Write
            for ac in ["BE", "BK", "VI", "VO"]:
                row = [n, rate, ac]
                
                def get_avg(k):
                    vals = stats_agg[ac][k]
                    return sum(vals) / len(vals) if vals else 0.0
                
                row.append(f"{get_avg('MAC_Throughput_Mbps'):.5f}")
                row.append(f"{get_avg('Loss_Pct'):.5f}")
                row.append(f"{get_avg('Retransmissions'):.5f}")
                row.append(f"{get_avg('MAC_Access_Delay_us'):.5f}")
                row.append(f"{get_avg('Queue_Delay_us'):.5f}")
                row.append(f"{get_avg('Access_Delay_us'):.5f}")

                writer.writerow(row)
                csvfile.flush()

print("Sweep Completed.")
