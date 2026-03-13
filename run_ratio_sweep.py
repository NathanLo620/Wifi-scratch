import subprocess
import re
import sys
import threading
import queue
import statistics
import csv
import os

# Configuration
sta_counts = range(2, 41, 2)
ratios = [0.1, 0.05, 0.01]  # 1/10, 1/20, 1/100
runs_per_point = 10
sim_time = 10.0
max_threads = 20
ns3_path = "./ns3"
csv_filename = "scratch/pedca_ratio_sweep_results_2Mbps.csv"

# Common base arguments
base_args = f"--totalDataRate=2Mbps --singleVoNode=true --simTime={sim_time}"

task_queue = queue.Queue()
results_lock = threading.Lock()

# Dictionary to store partial results for aggregation
# Key: (ratio, nSta, scenario)
# Value: List of result dicts
aggregated_results = {}

# Initialize CSV
with open(csv_filename, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(["VO_Ratio", "nSta", "Scenario", "Avg_Delay_us", "Avg_Throughput_Mbps", "Avg_Loss_Percent", "Avg_Retx_Ratio"])

def parse_metrics(output):
    metrics = {
        "delay": 0.0,
        "throughput": 0.0,
        "loss": 0.0,
        "retx": 0.0
    }
    # Find AC_VO section
    vo_section = re.search(r"AC_VO:(.*?)(?:AC_|$)", output, re.DOTALL)
    if vo_section:
        vo_text = vo_section.group(1)
        
        # MAC Delay
        m = re.search(r"Avg MAC Delay:\s+([\d\.]+)", vo_text)
        if m: metrics["delay"] = float(m.group(1))
        
        # Throughput
        m = re.search(r"Throughput:\s+([\d\.]+)", vo_text)
        if m: metrics["throughput"] = float(m.group(1))

        # Packet Loss
        m = re.search(r"Packet Loss:\s+([\d\.]+)", vo_text)
        if m: metrics["loss"] = float(m.group(1))

        # Retx
        m = re.search(r"Avg Retx/MPDU:\s+([\d\.]+)", vo_text)
        if m: metrics["retx"] = float(m.group(1))
        
        return metrics
    return None

def worker():
    while True:
        try:
            task = task_queue.get(timeout=1)
        except queue.Empty:
            break
            
        ratio, scenario, nSta, runId = task
        
        cmd_str = f"{scenario} --nSta={nSta} --RngRun={runId} --voRatio={ratio} {base_args}"
        cmd = [ns3_path, "run", "--no-build", cmd_str]
        
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            metrics = parse_metrics(res.stdout)
            
            if metrics:
                with results_lock:
                    key = (ratio, nSta, scenario)
                    if key not in aggregated_results:
                        aggregated_results[key] = []
                    
                    aggregated_results[key].append(metrics)
                    
                    # If we have collected all runs for this point, calculate average and write to CSV
                    if len(aggregated_results[key]) == runs_per_point:
                        results_list = aggregated_results[key]
                        
                        avg_delay = statistics.mean([m["delay"] for m in results_list])
                        avg_thr = statistics.mean([m["throughput"] for m in results_list])
                        avg_loss = statistics.mean([m["loss"] for m in results_list])
                        avg_retx = statistics.mean([m["retx"] for m in results_list])
                        
                        # Write to file
                        with open(csv_filename, 'a', newline='') as f:
                            writer = csv.writer(f)
                            writer.writerow([
                                ratio, nSta, scenario,
                                f"{avg_delay:.4f}", f"{avg_thr:.4f}", 
                                f"{avg_loss:.4f}", f"{avg_retx:.4f}"
                            ])
                        
                        # Clean up memory
                        del aggregated_results[key]
                        
        except Exception as e:
            # print(f"Error {cmd_str}: {e}")
            pass
        
        task_queue.task_done()

# 1. Build
print("Building targets...")
subprocess.run([ns3_path, "build", "scratch/pedca_scenario", "scratch/edca_scenario"], check=True)

# 2. Queue Tasks
total_tasks = len(ratios) * len(sta_counts) * runs_per_point * 2
print(f"Queueing {total_tasks} simulation tasks...")

for ratio in ratios:
    for nSta in sta_counts:
        for r in range(1, runs_per_point + 1):
            task_queue.put((ratio, "pedca_scenario", nSta, r))
            task_queue.put((ratio, "edca_scenario", nSta, r))

# 3. Exec
print(f"Starting execution with {max_threads} threads. Averaged output will be saved to {csv_filename}")
threads = []
for _ in range(max_threads):
    t = threading.Thread(target=worker)
    t.start()
    threads.append(t)

# Main thread finishes here, but threads keep running. 
# Wait for them to finish.
for t in threads:
    t.join()

print("All simulations completed.")
