import time
import pandas as pd
import joblib
import subprocess

print("[*] Loading Machine Learning Model...")
clf = joblib.load('rf_model.pkl')

def observe_system():
    # Measure over 1 full second
    cmd_core = [
        "sudo", "perf", "stat", "-x", ",", "-C", "0", "-e",
        "cycles,instructions,LLC-loads,LLC-load-misses",
        "sleep", "1"
    ]
    cmd_uncore = [
        "sudo", "perf", "stat", "-x", ",", "-a", "-e",
        "uncore_imc_0/cas_count_read/,uncore_imc_0/cas_count_write/,"
        "uncore_imc_1/cas_count_read/,uncore_imc_1/cas_count_write/,"
        "uncore_imc_2/cas_count_read/,uncore_imc_2/cas_count_write/,"
        "uncore_imc_3/cas_count_read/,uncore_imc_3/cas_count_write/,"
        "uncore_imc_4/cas_count_read/,uncore_imc_4/cas_count_write/,"
        "uncore_imc_5/cas_count_read/,uncore_imc_5/cas_count_write/",
        "sleep", "1"
    ]

    p_core = subprocess.Popen(cmd_core, stderr=subprocess.PIPE, text=True)
    p_uncore = subprocess.Popen(cmd_uncore, stderr=subprocess.PIPE, text=True)
    
    _, err_c = p_core.communicate()
    _, err_u = p_uncore.communicate()

    metrics = {'cycles': 1, 'instructions': 0, 'LLC-loads': 0, 'LLC-load-misses': 0, 'cas_count_total': 0}
    
    for line in err_c.splitlines():
        parts = line.split(',')
        if len(parts) >= 3:
            try:
                val = float(parts[0].replace('<not counted>', '0'))
                event = parts[2]
                if 'cycles' in event: metrics['cycles'] = val
                elif 'instructions' in event: metrics['instructions'] = val
                elif 'LLC-loads' in event: metrics['LLC-loads'] = val
                elif 'LLC-load-misses' in event: metrics['LLC-load-misses'] = val
            except Exception: 
                continue

    for line in err_u.splitlines():
        parts = line.split(',')
        if len(parts) >= 3:
            try:
                val = float(parts[0].replace('<not counted>', '0'))
                unit = parts[1]
                event = parts[2]
                
                # Reverse-engineer perf's auto-scaling back to raw CAS counts!
                if 'MiB' in unit: val = (val * 1048576) / 64
                elif 'KiB' in unit: val = (val * 1024) / 64
                elif 'B' in unit or 'Bytes' in unit: val = val / 64
                elif 'GiB' in unit: val = (val * 1073741824) / 64

                if 'cas_count' in event: 
                    metrics['cas_count_total'] += val
            except Exception: 
                continue

    # Scale the 1-second data down by 10 to perfectly match the 100ms training data distribution
    metrics['cycles'] /= 10
    metrics['instructions'] /= 10
    metrics['LLC-loads'] /= 10
    metrics['LLC-load-misses'] /= 10
    metrics['cas_count_total'] /= 10

    # Feature Engineering
    ipc = metrics['instructions'] / metrics['cycles'] if metrics['cycles'] > 0 else 0
    total_mb_per_sec = (metrics['cas_count_total'] * 64 / 1048576) * 10 
    
    llc_intensity = metrics['LLC-loads'] / metrics['cycles'] if metrics['cycles'] > 0 else 0
    miss_rate = metrics['LLC-load-misses'] / metrics['LLC-loads'] if metrics['LLC-loads'] > 0 else 0
    cache_dominance = llc_intensity * (1 - miss_rate)
    
    return [ipc, total_mb_per_sec, cache_dominance, metrics['LLC-load-misses'], metrics['LLC-loads']]

print("[*] Starting Closed-Loop Mitigation Daemon...")
try:
    while True:
        features = observe_system()
        df_live = pd.DataFrame([features], columns=[
            'IPC', 'Total_MB_per_sec', 'cache_dominance_score', 'LLC-load-misses', 'LLC-loads'
        ])
        
        prediction = clf.predict(df_live)[0]
        stats = f"MB/s: {features[1]:.0f} | LLC-Misses: {features[3]:.0f} | IPC: {features[0]:.2f}"
        
        if prediction == 1:
            print(f"\n[!] WARNING: Noisy-Neighbor Detected! ({stats})")
            print("[*] Engaging Mitigation Shield via Intel RDT...")
            subprocess.run(["python3", "mitigation_shield.py", "--attacker_core", "1", "--action", "isolate"])
            
            print("[*] Sleeping for 10 seconds to allow system stabilization...")
            time.sleep(10)
            
            print("[*] Restoring Baseline...")
            subprocess.run(["python3", "mitigation_shield.py", "--attacker_core", "1", "--action", "baseline"])
        else:
            print(f"[✓] System Stable. ({stats})                   ", end="\r")
            
except KeyboardInterrupt:
    print("\n[*] Daemon offline.")
