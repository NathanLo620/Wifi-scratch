import subprocess
import re
import sys
import os
import csv

# Configuration
n_stas = [10, 20]
data_rates = ["0.1Mbps", "0.5Mbps", "1Mbps", "5Mbps", "10Mbps", "50Mbps"]
sim_sec = 10
ns3_path = os.path.abspath("../ns3")
output_csv = "sweep_results_80211n.csv"

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
    stats["Retries"] = parse_val(r"Avg Retries/Pkt:\s+([\d\.]+)", block)
    stats["QueueDelay"] = parse_val(r"Avg Queue Delay:\s+([\d\.]+) us", block)
    stats["AirDelay"] = parse_val(r"Avg Air Delay:\s+([\d\.]+) us", block)
    return stats

# Main Sweep
print(f"Starting Sweep. Results will be saved to {output_csv}")
print("nSta, DataRate, AC, Throughput(Mbps), Loss(%), Retries, QueueDelay(us), AirDelay(us)")

with open(output_csv, 'w', newline='') as csvfile:
    writer = csv.writer(csvfile)
    writer.writerow(["nSta", "DataRate", "AC", "Throughput_Mbps", "Loss_Pct", "Retries", "QueueDelay_us", "AirDelay_us"])

    for n in n_stas:
        for rate in data_rates:
            print(f"Running nSta={n}, Rate={rate}...")
            cmd = [ns3_path, "run", f"wifi_backoff80211n --nSta={n} --dataRate={rate} --sim={sim_sec}"]
            
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd(), check=True)
                output = proc.stdout
                
                scenario_data = {} # store for plotting
                
                for ac in ["BE", "BK", "VI", "VO"]:
                    stats = parse_ac_stats(output, ac)
                    if not stats:
                        continue
                        
                    # Write to CSV
                    writer.writerow([n, rate, ac, stats["Throughput"], stats["Loss"], stats["Retries"], stats["QueueDelay"], stats["AirDelay"]])
                    csvfile.flush()
                    
                    scenario_data[ac] = stats
                
                # Plotting with Gnuplot
                if scenario_data:
                    acs = ["BE", "BK", "VI", "VO"]
                    throughputs = [scenario_data.get(ac, {}).get("Throughput", 0) for ac in acs]
                    
                    # Create Data File
                    dat_file = f"plot_n{n}_{rate}.dat"
                    with open(dat_file, 'w') as f:
                        f.write("AC Throughput\n")
                        for i, ac in enumerate(acs):
                            f.write(f"{ac} {throughputs[i]}\n")
                    
                    # Create Gnuplot Script
                    plt_file = f"plot_n{n}_{rate}.plt"
                    png_file = f"plot_n{n}_{rate}.png"
                    
                    with open(plt_file, 'w') as f:
                        f.write(f"set terminal pngcairo size 800,600 enhanced font 'Verdana,10'\n")
                        f.write(f"set output '{png_file}'\n")
                        f.write(f"set title '802.11n EDCA Performance (nSta={n}, Rate={rate})'\n")
                        f.write("set style data histograms\n")
                        f.write("set style fill solid 1.0 border -1\n")
                        f.write("set ylabel 'Throughput (Mbps)'\n")
                        f.write("set xlabel 'Access Category'\n")
                        f.write("set yrange [0:*]\n")
                        f.write("set grid y\n")
                        f.write(f"plot '{dat_file}' using 2:xtic(1) title 'Throughput' lc rgb '#3498db'\n")
                    
                    # Run Gnuplot
                    subprocess.run(["gnuplot", plt_file], check=True)
                    
            except subprocess.CalledProcessError as e:
                print(f"Error running n={n}, rate={rate}: {e}")
            except Exception as e:
                print(f"Error processing n={n}, rate={rate}: {e}")

print("Sweep Completed.")
