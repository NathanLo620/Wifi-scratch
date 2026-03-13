#!/usr/bin/env python3
"""
P-EDCA Sweep Test Script
Runs pedca_verification_nsta.cc with varying STA counts and collects statistics.
"""
import subprocess
import os
import csv
import re
import collections

# Configuration
n_sta_list = list(range(2, 41, 2))  # 2, 4, 6, ..., 40
sim_time = 10.0
data_rate = "1Mbps"
output_dir = "scratch/PEDCA_result"
output_file = f"{output_dir}/PEDCA_Sweep_Sta_{data_rate}.csv"

# Number of Monte Carlo runs for averaging
n_runs = 10

def ensure_dir(d):
    if not os.path.exists(d):
        os.makedirs(d)

def run_simulation(n_sta, run_idx):
    """Run a single simulation with given parameters."""
    cmd = [
        "./ns3", "run",
        f"scratch/pedca_verification_nsta.cc --nSta={n_sta} --simTime={sim_time} --dataRate={data_rate} --RngRun={run_idx}"
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd="../")
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error running simulation nSta={n_sta} Run={run_idx}: {e}")
        return None

def parse_val(pattern, text, default=0.0):
    m = re.search(pattern, text)
    if m:
        return float(m.group(1))
    return default

def parse_results(stdout):
    """
    Parse simulation output using Regex for block-based format.
    """
    results = {}
    
    # Parse Helper Stats Block
    helper_ac_blocks = re.findall(r"(AC_[A-Z]{2}):\n\s+Successes:(.*?)(?=AC_|---|$)", stdout, re.DOTALL)
    
    for ac_label, block in helper_ac_blocks:
        ac = ac_label.replace("AC_", "")
        results[ac] = {
            'Throughput_Mbps': parse_val(r"Throughput:\s+([\d\.]+) Mbps", block),
            'Packet_Loss_Pct': parse_val(r"Packet Loss:\s+([\d\.]+) %", block),
            'Queue_Delay_us': parse_val(r"Avg Queue Delay:\s+([\d\.]+) us", block),
            'Access_Delay_us': parse_val(r"Avg Access Delay:\s+([\d\.]+) us", block),
            'Retransmission_Ratio': parse_val(r"Avg Retx/MPDU:\s+([\d\.]+)", block)
        }
    
    return results

def main():
    ensure_dir(f"../{output_dir}")
    full_output_path = f"../{output_file}"
    
    headers = [
        "N_Sta", 
        "Access_Category", 
        "Throughput_Mbps", 
        "Packet_Loss_Pct", 
        "Queue_Delay_us", 
        "Access_Delay_us",
        "Retransmission_Ratio"
    ]
    
    # Initialize CSV with headers
    with open(full_output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
    
    print(f"P-EDCA Sweep Test")
    print(f"  STA range: {n_sta_list[0]} to {n_sta_list[-1]}")
    print(f"  Runs per config: {n_runs}")
    print(f"  Output: {full_output_path}")
    print("-" * 50)
    
    for n in n_sta_list:
        print(f"Simulating nSta={n} ({n_runs} runs)...", end=" ", flush=True)
        
        # Accumulate results for averaging
        sums = {
            ac: {k: 0.0 for k in ['Throughput_Mbps', 'Packet_Loss_Pct', 'Queue_Delay_us', 'Access_Delay_us', 'Retransmission_Ratio']}
            for ac in ["VO", "VI", "BE", "BK"]
        }
        valid_runs = 0
        
        for r in range(1, n_runs + 1):
            stdout = run_simulation(n, r)
            if stdout:
                parsed = parse_results(stdout)
                if len(parsed) >= 1:  # At least one AC parsed
                    valid_runs += 1
                    for ac, vals in parsed.items():
                        for key in sums[ac]:
                            sums[ac][key] += vals.get(key, 0.0)
        
        if valid_runs > 0:
            # Average and Write
            with open(full_output_path, 'a', newline='') as f:
                writer = csv.writer(f)
                for ac in ["VO", "VI", "BE", "BK"]:
                    if all(sums[ac][k] == 0 for k in sums[ac]):
                        continue  # Skip if all zeros
                    avgs = {k: sums[ac][k] / valid_runs for k in sums[ac]}
                    row = [
                        n,
                        ac,
                        f"{avgs['Throughput_Mbps']:.4f}",
                        f"{avgs['Packet_Loss_Pct']:.4f}",
                        f"{avgs['Queue_Delay_us']:.2f}",
                        f"{avgs['Access_Delay_us']:.2f}",
                        f"{avgs['Retransmission_Ratio']:.4f}"
                    ]
                    writer.writerow(row)
            print(f"OK ({valid_runs} valid runs)")
        else:
            print(f"FAILED (no valid runs)")

    print("-" * 50)
    print(f"Results saved to {full_output_path}")

if __name__ == "__main__":
    main()
