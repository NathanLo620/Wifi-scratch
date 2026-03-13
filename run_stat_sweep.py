import subprocess
import re
import sys
import threading
import queue
import time
import statistics

# Configuration
sta_counts = range(2, 51, 2) # 2, 4, ..., 40
runs_per_point = 5
sim_time = 4.0 # Reduced simulation time to speed up batch processing
common_args = f"--totalDataRate=2Mbps --singleVoNode=true --voRatio=0.1 --simTime={sim_time}"
ns3_path = "./ns3"

# Threading
max_threads = 20
task_queue = queue.Queue()
results_lock = threading.Lock()

# Storage
# results[nSta]['pedca'] = [delay_run1, delay_run2, ...]
results = {n: {'pedca': [], 'edca': []} for n in sta_counts}

def parse_delay(output):
    # Find AC_VO section
    vo_section = re.search(r"AC_VO:(.*?)(?:AC_|$)", output, re.DOTALL)
    if vo_section:
        vo_text = vo_section.group(1)
        mac_delay = re.search(r"Avg MAC Delay:\s+([\d\.]+)", vo_text)
        if mac_delay:
            return float(mac_delay.group(1))
    return None

def worker():
    while True:
        try:
            task = task_queue.get(timeout=1)
        except queue.Empty:
            break
            
        scenario, nSta, runId = task
        # Use --no-build to avoid lock contention and speed up start
        cmd = [ns3_path, "run", "--no-build", f"{scenario} --nSta={nSta} --RngRun={runId} {common_args}"]
        
        try:
            # print(f"Start {scenario} n={nSta} r={runId}")
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            delay = parse_delay(res.stdout)
            
            if delay is not None:
                with results_lock:
                    key = 'pedca' if 'pedca' in scenario else 'edca'
                    results[nSta][key].append(delay)
            # else:
            #     print(f"No delay found for {scenario} n={nSta} r={runId}")

        except Exception as e:
            print(f"Check failed {scenario} n={nSta} r={runId}: {e}")
        
        task_queue.task_done()

# 1. Build first explicitly to ensure binaries exist
print("Building targets...")
subprocess.run([ns3_path, "build", "scratch/pedca_scenario", "scratch/edca_scenario"], check=True)

# 2. Populate queue
print(f"Queueing tasks ({len(sta_counts)} points * {runs_per_point} runs * 2 scenarios)...")
for nSta in sta_counts:
    for r in range(1, runs_per_point + 1):
        task_queue.put(("pedca_scenario", nSta, r))
        task_queue.put(("edca_scenario", nSta, r))

# 3. Start threads
print(f"Starting execution with {max_threads} threads...")
threads = []
for _ in range(max_threads):
    t = threading.Thread(target=worker)
    t.start()
    threads.append(t)

# 4. Wait
for t in threads:
    t.join()

# 5. Report
print("\n" + "="*80)
print(f"{'nSta':<5} | {'P-EDCA Avg(us)':<15} | {'EDCA Avg(us)':<15} | {'Diff (%)':<10} | {'Winner':<10}")
print("-" * 80)

for nSta in sorted(sta_counts):
    p_delays = results[nSta]['pedca']
    e_delays = results[nSta]['edca']
    
    if p_delays and e_delays:
        p_avg = statistics.mean(p_delays)
        e_avg = statistics.mean(e_delays)
        
        # Improvement: How much lower is P-EDCA compared to EDCA?
        # (EDCA - PEDCA) / EDCA
        diff = ((e_avg - p_avg) / e_avg) * 100
        
        winner = "P-EDCA" if p_avg < e_avg else "EDCA"
        
        print(f"{nSta:<5} | {p_avg:<15.2f} | {e_avg:<15.2f} | {diff:<10.2f} | {winner:<10}")
    else:
        print(f"{nSta:<5} | {'N/A':<15} | {'N/A':<15} | {'N/A':<10} | Incomplete")

print("="*80)
