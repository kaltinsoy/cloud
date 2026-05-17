import subprocess
import pandas as pd
import sys
import argparse
import os

# --- 1. CONFIGURATION ---
parser = argparse.ArgumentParser(description='Collect Noisy Neighbor Telemetry with IMC bandwidth.')
parser.add_argument('--victim', type=str, required=True, help='Name of the victim workload')
parser.add_argument('--attacker', type=str, required=True, help='Name of the attacker workload')
parser.add_argument('--run_id', type=str, required=True, help='Run iteration number')
args = parser.parse_args()

MONITOR_CORE = "0"
INTERVAL_MS = "100"
DURATION_SEC = "60"

CORE_EVENTS = "instructions,cycles,LLC-load-misses,LLC-loads"
UNCORE_EVENTS = (
    "uncore_imc_0/cas_count_read/,uncore_imc_0/cas_count_write/,"
    "uncore_imc_1/cas_count_read/,uncore_imc_1/cas_count_write/,"
    "uncore_imc_2/cas_count_read/,uncore_imc_2/cas_count_write/,"
    "uncore_imc_3/cas_count_read/,uncore_imc_3/cas_count_write/,"
    "uncore_imc_4/cas_count_read/,uncore_imc_4/cas_count_write/,"
    "uncore_imc_5/cas_count_read/,uncore_imc_5/cas_count_write/"
)

# --- 2. ROBUST DATA PARSER ---
def parse_perf_csv(filepath):
    records = []
    if not os.path.exists(filepath):
        return records
        
    with open(filepath, 'r') as f:
        for line in f:
            if line.startswith('#'): continue
            parts = [p.strip() for p in line.split(',')]
            
            # Make sure line has data and isn't "not counted"
            if len(parts) >= 4 and parts[1] not in ['<not counted>', '<not supported>', '']:
                try:
                    # Use float() to prevent crashing on kernel decimals (e.g., "0.50")
                    val = float(parts[1])
                    unit = parts[2]
                    event_name = parts[3]
                    
                    # Reverse-engineer perf's auto-scaling to keep our math consistent
                    if 'MiB' in unit: val = (val * 1048576) / 64
                    elif 'KiB' in unit: val = (val * 1024) / 64
                    elif 'B' in unit or 'Bytes' in unit: val = val / 64
                    elif 'GiB' in unit: val = (val * 1073741824) / 64

                    records.append({
                        "timestamp": float(parts[0]),
                        "event": event_name,
                        "value": val
                    })
                except Exception as e:
                    print(f"[-] Ignored bad row: {line.strip()} -> {e}")
                    continue
    return records

# --- 3. THE EXECUTION ENGINE ---
print(f"[*] Recording {args.victim} vs {args.attacker} (Run {args.run_id}) for {DURATION_SEC}s...")

# Removed 'sudo' from the command lists because Python is already running as root
cmd_core = [
    "perf", "stat", 
    "-I", INTERVAL_MS, "-x", ",", "-C", MONITOR_CORE, 
    "-e", CORE_EVENTS, "-o", "core_temp.csv", "sleep", DURATION_SEC
]

cmd_uncore = [
    "perf", "stat", 
    "-I", INTERVAL_MS, "-x", ",", "-a", 
    "-e", UNCORE_EVENTS, "-o", "uncore_temp.csv", "sleep", DURATION_SEC
]

try:
    # Run both tracepoints simultaneously and capture any errors
    p_core = subprocess.Popen(cmd_core, stderr=subprocess.PIPE)
    p_uncore = subprocess.Popen(cmd_uncore, stderr=subprocess.PIPE)
    
    p_core.wait()
    p_uncore.wait()
    
except KeyboardInterrupt:
    print("\n[*] Interrupted. Terminating telemetry...")
    p_core.terminate()
    p_uncore.terminate()
    sys.exit(1)

print("[*] Telemetry collection finished. Processing dual-stream data...")

# --- 4. DATA PROCESSING & MATH ---
core_data = parse_perf_csv("core_temp.csv")
uncore_data = parse_perf_csv("uncore_temp.csv")

if not core_data or not uncore_data:
    print("[-] Error: Missing data streams. Perf failed to execute.")
    # If it fails, print exactly what the OS complained about
    _, err_c = p_core.communicate()
    _, err_u = p_uncore.communicate()
    if err_c: print(f"\nCore Trace Error:\n{err_c.decode('utf-8')}")
    if err_u: print(f"\nUncore Trace Error:\n{err_u.decode('utf-8')}")
    sys.exit(1)

df_c = pd.DataFrame(core_data)
df_u = pd.DataFrame(uncore_data)

# Round to 1 decimal place (e.g. 0.1) so Core and Uncore timestamps align perfectly
df_c['time_step'] = df_c['timestamp'].round(1)
df_u['time_step'] = df_u['timestamp'].round(1)

df_core_pivot = df_c.pivot_table(index="time_step", columns="event", values="value").reset_index()

# Sum all 6 memory channels together
df_u_grouped = df_u.groupby('time_step')['value'].sum().reset_index()
df_u_grouped.rename(columns={'value': 'cas_count_total'}, inplace=True)

df_master = pd.merge(df_core_pivot, df_u_grouped, on="time_step", how="inner")

if 'instructions' in df_master.columns and 'cycles' in df_master.columns:
    df_master['IPC'] = df_master['instructions'] / df_master['cycles']

if 'cas_count_total' in df_master.columns:
    df_master['Total_MB_per_sec'] = (df_master['cas_count_total'] * 64 / 1048576) * 10

if 'LLC-loads' in df_master.columns and 'LLC-load-misses' in df_master.columns and 'cycles' in df_master.columns:
    llc_intensity = df_master['LLC-loads'] / df_master['cycles']
    miss_rate = df_master['LLC-load-misses'] / df_master['LLC-loads']
    miss_rate = miss_rate.fillna(0) 
    df_master['cache_dominance_score'] = llc_intensity * (1 - miss_rate)

df_master['victim'] = args.victim
df_master['attacker'] = args.attacker
df_master['run_id'] = args.run_id

# Organize the final columns
columns_order = [
    'time_step', 'victim', 'attacker', 'run_id', 'IPC', 'Total_MB_per_sec', 
    'cache_dominance_score', 'LLC-load-misses', 'LLC-loads', 'instructions', 'cycles', 'cas_count_total'
]
columns_order = [col for col in columns_order if col in df_master.columns]
df_master = df_master[columns_order]

output_file = f"data_{args.victim}_vs_{args.attacker}_run{args.run_id}.csv"
df_master.to_csv(output_file, index=False)

if os.path.exists("core_temp.csv"): os.remove("core_temp.csv")
if os.path.exists("uncore_temp.csv"): os.remove("uncore_temp.csv")

print(f"[+] Success! {len(df_master)} fused samples saved to {output_file}")
print("\nSample Output:")
print(df_master[['time_step', 'IPC', 'Total_MB_per_sec', 'cache_dominance_score']].head())
