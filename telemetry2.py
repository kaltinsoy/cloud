import subprocess
import time
import pandas as pd
import sys

# We are monitoring Core 0, where we will pin our victim workload
MONITOR_CORE = "0"
INTERVAL_MS = "100"
DURATION_SEC = 10

# The hardware events we want to track
EVENTS = "instructions,cycles,LLC-load-misses"

def run_telemetry():
    print(f"[*] Starting Telemetry Daemon on Core {MONITOR_CORE} for {DURATION_SEC} seconds...")
    
    # Launch perf in interval mode (-I), CSV format (-x,), attaching to specific core (-C)
    cmd = [
        "sudo", "perf", "stat", 
        "-I", INTERVAL_MS, 
        "-x", ",", 
        "-C", MONITOR_CORE, 
        "-e", EVENTS
    ]
    
    # Start the process and capture the stream
    process = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
    
    data_records = []
    start_time = time.time()
    
    try:
        # Read the stream line by line as it generates every 100ms
        for line in process.stderr:
            # Example perf CSV line: 0.100123,15000,,instructions,,,
            parts = [p.strip() for p in line.split(',')]
            
            # Ensure the line has enough data columns
            if len(parts) >= 4 and parts[1] != '<not counted>':
                try:
                    timestamp = float(parts[0])
                    value = int(parts[1])
                    event_name = parts[3]
                    
                    data_records.append({
                        "timestamp": timestamp,
                        "event": event_name,
                        "value": value
                    })
                except ValueError:
                    continue # Skip header lines or malformed rows
            
            # Stop if we hit our duration
            if (time.time() - start_time) > DURATION_SEC:
                break
                
    except KeyboardInterrupt:
        print("[*] Interrupted by user.")
    
    finally:
        process.terminate()
        print("[*] Telemetry collection finished. Processing data...")
        
    return data_records

if __name__ == "__main__":
    raw_data = run_telemetry()
    
    if not raw_data:
        print("[-] No data collected. Did you run with sudo privileges?")
        sys.exit(1)
        
    # Convert the raw list of dictionaries into a Pandas DataFrame
    df = pd.DataFrame(raw_data)
    
    # Pivot the table so each timestamp is a row, and events are columns
    df_pivot = df.pivot_table(index="timestamp", columns="event", values="value").reset_index()
    
    # Calculate IPC (Instructions Per Cycle)
    if 'instructions' in df_pivot.columns and 'cycles' in df_pivot.columns:
        df_pivot['IPC'] = df_pivot['instructions'] / df_pivot['cycles']
    
    # Save the cleaned dataset
    output_file = "baseline_telemetry.csv"
    df_pivot.to_csv(output_file, index=False)
    
    print(f"[+] Dataset saved to {output_file}")
    print("\nSample Data:")
    print(df_pivot.head())
