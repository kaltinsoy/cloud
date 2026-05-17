#!/bin/bash

# Define the experiment matrix
VICTIMS=("sysbench" "redis-benchmark")
ATTACKERS=("none" "STREAM" "stress-ng")
RUNS=3 # Number of 60-second samples per combination

echo "[*] Starting Automated Data Collection Factory..."

for victim in "${VICTIMS[@]}"; do
    for attacker in "${ATTACKERS[@]}"; do
        for run in $(seq 1 $RUNS); do
            echo "---------------------------------------------------"
            echo "[*] Launching Victim: $victim | Attacker: $attacker | Run: $run"
            
            # --- 1. LAUNCH THE ATTACKER (Core 1) ---
            if [ "$attacker" == "STREAM" ]; then
                # Loop STREAM infinitely on Core 1
                while true; do numactl --physcpubind=1 --membind=0 ~/cloud/STREAM/stream_c.exe > /dev/null; done &
                ATTACKER_PID=$!
            elif [ "$attacker" == "stress-ng" ]; then
                # Launch a heavy matrix math / cache destroyer on Core 1
                numactl --physcpubind=1 --membind=0 stress-ng --matrix 1 --cache 1 > /dev/null &
                ATTACKER_PID=$!
            else
                # Baseline run (No attacker)
                ATTACKER_PID=""
            fi

            # --- 2. LAUNCH THE VICTIM (Core 0) ---
            if [ "$victim" == "sysbench" ]; then
                # Loop CPU calculations infinitely on Core 0
                while true; do numactl --physcpubind=0 --membind=0 sysbench cpu --cpu-max-prime=50000 run > /dev/null; done &
                VICTIM_PID=$!
            elif [ "$victim" == "redis-benchmark" ]; then
                # Loop intense Redis database transactions infinitely on Core 0
                while true; do numactl --physcpubind=0 --membind=0 redis-benchmark -t set,get -n 100000000 -q > /dev/null; done &
                VICTIM_PID=$!
            fi

            # --- 3. RUN TELEMETRY (60 Seconds) ---
            # Wait 2 seconds for workloads to stabilize, then record!
            sleep 2
            sudo ~/cloud/telemetry_env/bin/python ~/cloud/telemetry.py --victim "$victim" --attacker "$attacker" --run_id "$run"

            # --- 4. CLEANUP FOR NEXT RUN ---
            echo "[*] Tearing down workloads..."
            # Kill the infinite loops and their background processes
            kill -9 $VICTIM_PID 2>/dev/null
            pkill -9 -P $VICTIM_PID 2>/dev/null
            pkill -9 sysbench 2>/dev/null
            pkill -9 redis-benchmark 2>/dev/null
            
            if [ -n "$ATTACKER_PID" ]; then
                kill -9 $ATTACKER_PID 2>/dev/null
                pkill -9 -P $ATTACKER_PID 2>/dev/null
                pkill -9 stream_c 2>/dev/null
                pkill -9 stress-ng 2>/dev/null
            fi
            
            # Let the CPU cache and memory controllers cool down to prevent cross-contamination
            echo "[*] Cooling down for 5 seconds..."
            sleep 5
        done
    done
done

echo "[+] All 18 experiments completed successfully! Check your directory for CSVs."
