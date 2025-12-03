import subprocess
import re
import sys
import os

# Configuration
n_stas = [5, 10, 20, 50]
cw_mins = [7, 15, 31, 63, 127, 255, 511, 1023]
sim_sec = [10]
ns3_path = os.path.abspath("../ns3")

# Helper to format
def fmt(val, prec=2):
    if isinstance(val, str): return val
    return f"{val:.{prec}f}"

def parse_val(pattern, output):
    m = re.search(pattern, output)
    if m:
        return float(m.group(1))
    return "N/A"

for sim in sim_sec:
    print(f"\n### Simulation Time: {sim}s (Saturated Throughput: 12.25Mbps Total)")
    print("| nSta | DataRate (Mbps) | CWmin | Retransmissions | Queue Delay (us) | Air Delay (us) | Packet Loss (%) | Throughput (Mbps) |")
    print("|---|---|---|---|---|---|---|---|")
    
    for n in n_stas:
        # Calculate rate per STA to achieve 12.25 Mbps total
        rate_val = 12.25 / n
        rate_str = f"{rate_val:.4f}Mbps"
        
        for cw in cw_mins:
            cmd = [ns3_path, "run", f"wifi_backoff --nSta={n} --cwMin={cw} --sim={sim} --rate={rate_str}"]
            
            try:
                # Run ns-3
                result = subprocess.run(
                    cmd, 
                    capture_output=True, 
                    text=True, 
                    cwd=os.getcwd(),
                    check=True
                )
                
                output = result.stdout
                
                # Parse results
                retries = parse_val(r"Avg retransmissions/pkt:\s+([\d\.]+)", output)
                delay = parse_val(r"Avg MAC Queue Delay \(Enq->1stTx\):\s+([\d\.]+)", output)
                air_delay = parse_val(r"Avg MAC Air Delay \(TxBegin->RxEndOk\):\s+([\d\.]+)", output)
                loss = parse_val(r"MAC Packet Loss Rate:\s+([\d\.]+)", output)
                throughput = parse_val(r"Throughput=([\d\.]+)", output)
                
                print(f"| {n} | {rate_val:.4f} | {cw} | {fmt(retries, 4)} | {fmt(delay)} | {fmt(air_delay)} | {fmt(loss)} | {fmt(throughput)} |")
                sys.stdout.flush()
                
            except subprocess.CalledProcessError as e:
                print(f"| {n} | {rate_val:.4f} | {cw} | Error | NS-3 exited with code {e.returncode} | N/A | N/A | N/A |")
                # print(e.stderr) # Debug
            except Exception as e:
                print(f"| {n} | {rate_val:.4f} | {cw} | Error | {e} | N/A | N/A | N/A |")

print("\nDone.")
