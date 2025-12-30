import subprocess
import re
import sys
import os
import csv

# Configuration
n_stas = list(range(2, 21, 2)) # 2 to 10
data_rates = ["1Mbps"]
sim_sec = 100
ns3_path = os.path.abspath("../ns3")
output_csv = "EDCA_Sweep_Sta_1Mbps.csv"

# Result Storage
results = []

def parse_val(pattern, text):
    m = re.search(pattern, text)
    if m:
        return float(m.group(1))
    return 0.0

def parse_ac_stats(output, ac_name):
    """Parses stats for a specific AC block from stdout"""
    # Regex to find the block for specific AC
    # e.g. --- AC_BE --- ... (until next --- or end)
    pattern = re.compile(f"--- AC_{ac_name} ---(.*?)(?=--- AC_|$)", re.DOTALL)
    m = pattern.search(output)
    if not m:
        return {}
    
    block = m.group(1)
    
    stats = {}
    stats["Throughput"] = parse_val(r"Throughput:\s+([\d\.]+) Mbps", block)
    stats["Loss"] = parse_val(r"Packet Loss:\s+([\d\.]+) %", block)
    stats["Retransmissions"] = parse_val(r"Avg Retries/Pkt:\s+([\d\.]+)", block)
    stats["ServiceTime"] = parse_val(r"Avg E2E Delay:\s+([\d\.]+) us", block)
    stats["AirDelay"] = parse_val(r"Avg Air Delay:\s+([\d\.]+) us", block)
    return stats

# Main Sweep
print(f"Starting Sweep. Results will be saved to {output_csv}")
print("nSta, DataRate, AC, Throughput(Mbps), Loss(%), Retransmissions, ServiceTime(us), AirDelay(us)")

with open(output_csv, 'w', newline='') as csvfile:
    writer = csv.writer(csvfile)
    writer.writerow(["nSta", "DataRate", "AC", "Throughput_Mbps", "Loss_Pct", "Retransmissions", "E2E_Delay_us", "AirDelay_us"])

    for n in n_stas:
        for rate in data_rates:
            print(f"Running nSta={n}, Rate={rate} (Averaging 5 Runs)...")
            
            # Storage for aggregation [Run1, Run2, ...]
            # Structure: ac -> metric -> [values]
            aggregator = { ac: { "Throughput": [], "Loss": [], "Retransmissions": [], "E2E_Delay": [], "AirDelay": [] } for ac in ["BE", "BK", "VI", "VO"] }
            
            for run_idx in range(1, 6): # Run 1 to 5
                cmd = [ns3_path, "run", f"wifi_backoff80211n --nSta={n} --dataRate={rate} --sim={sim_sec} --RngRun={run_idx}"]
                
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd(), check=True)
                    output = proc.stdout
                    
                    for ac in ["BE", "BK", "VI", "VO"]:
                        stats = parse_ac_stats(output, ac)
                        if stats:
                            aggregator[ac]["Throughput"].append(stats["Throughput"])
                            aggregator[ac]["Loss"].append(stats["Loss"])
                            aggregator[ac]["Retransmissions"].append(stats["Retransmissions"])
                            aggregator[ac]["E2E_Delay"].append(stats["ServiceTime"]) # Parsed as ServiceTime key
                            aggregator[ac]["AirDelay"].append(stats["AirDelay"])

                except Exception as e:
                    print(f"Error running n={n}, rate={rate}, run={run_idx}: {e}")

            # Compute Average and Write
            for ac in ["BE", "BK", "VI", "VO"]:
                def avg(lst): return sum(lst) / len(lst) if lst else 0.0
                
                # Careful with empty lists if runs failed
                if not aggregator[ac]["Throughput"]: 
                    continue

                avg_tput = avg(aggregator[ac]["Throughput"])
                avg_loss = avg(aggregator[ac]["Loss"])
                avg_retries = avg(aggregator[ac]["Retransmissions"])
                avg_delay = avg(aggregator[ac]["E2E_Delay"])
                avg_air = avg(aggregator[ac]["AirDelay"])

                writer.writerow([n, rate, ac, f"{avg_tput:.5f}", f"{avg_loss:.5f}", f"{avg_retries:.5f}", f"{avg_delay:.5f}", f"{avg_air:.5f}"])
                csvfile.flush()
                

                
                # Plotting skipped as requested
                # if scenario_data:
                #    ...
                    


print("Sweep Completed.")
