```markdown
# Project: Noisy-Neighbor Detection & Active Defense (Closed-Loop Prototype)

**Authors:** Anıl Koray Altınsoy & Kazım Önses  
**Course:** Cloud Computing (Level 2)  
**Target Hardware:** Intel Xeon Gold 6136 (Skylake-SP)  

## Overview
This repository contains a full closed-loop ML-based system for detecting and mitigating "Noisy-Neighbor" (cache/memory bandwidth contention) attacks in cloud environments. The system continuously monitors CPU performance counters, detects interference using a trained Random Forest classifier, and automatically applies Intel RDT CAT (Cache Allocation Technology) to isolate the aggressor.

A live web dashboard (Flask + Socket.IO) visualizes telemetry, detected events, ML confidence, and mitigation effectiveness in real time.

## Key Features & v3 Updates
* **Hardware Erratum Workaround:** Due to Intel SKX114 Erratum (CMT/MBM disabled), we monitor per-core `perf_event` PMU counters (IPC, LLC misses) and system-wide Uncore IMC counters (MB/s) as a highly accurate proxy.
* **Smart Baseline Labeling:** The model triggers an attack **only** if the victim suffers a >20% IPC drop from its specific baseline. High cache misses alone do not trigger false positives if the workload naturally generates them (e.g., `mcf`).
* **Concurrent Live Daemon:** The `live_daemon.py` (v3) uses concurrent subprocesses and a 1-second sampling window (scaled to 100ms) to bypass Linux `perf` initialization overhead, ensuring highly accurate real-time data feeding to the ML model.
* **Hysteresis State Machine:** K=3 (trigger) and M=10 (release) filters prevent mitigation oscillation.

---

## Repository Structure

```text
.
├── app.py                       # Flask + Socket.IO backend for Web Dashboard
├── live_daemon.py               # Main detection + mitigation loop (v3 Concurrent)
├── telemetry.py                 # Offline perf data collection for training
├── train_model.py               # Model training (IPC-drop labeling)
├── feature_engineering.py       # Rolling window features, aggressor detection
├── mitigation_shield.py         # Intel pqos CAT/MBA wrapper
├── mitigation_verification.py   # Before/after IPC comparison logic
├── run_experiments.sh           # 4x4x10 Matrix Runner (Fixed subshell blocking)
├── templates/index.html         # 4-view dashboard UI (HTML/JS)
└── STREAM/stream_c.exe          # Compiled STREAM benchmark
```

---

## 1. Prerequisites & Setup

Run the following commands to configure your environment, compile necessary benchmarks, and generate dummy workloads.

### Install System Packages
```bash
sudo apt-get update
sudo apt-get install -y linux-tools-common linux-tools-generic \
                        intel-cmt-cat python3 python3-pip python3-venv \
                        sysbench stress-ng redis-tools x264 ffmpeg
```

### Compile STREAM Benchmark
```bash
mkdir -p ~/cloud2/STREAM
wget https://raw.githubusercontent.com/jeffhammond/STREAM/master/stream.c -O ~/cloud2/STREAM/stream.c
gcc -O2 -fopenmp ~/cloud2/STREAM/stream.c -o ~/cloud2/STREAM/stream_c.exe
```

### Create Dummy Workloads (mcf & x264 video)
Because SPEC CPU `mcf` is proprietary, we use a C program that mimics its heavy L3 cache-thrashing behavior.
```bash
# 1. Create test video for x264
ffmpeg -f lavfi -i testsrc=duration=30:size=1280x720:rate=30 -pix_fmt yuv420p /tmp/test_video.y4m -y

# 2. Create mcf_dummy.c
cat << 'EOF' > ~/cloud2/mcf_dummy.c
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char *argv[]) {
    size_t size = (256 * 1024 * 1024) / sizeof(int);
    volatile int *data = (int *)malloc(size * sizeof(int));
    if (!data) return 1;
    for (size_t i = 0; i < size; i++) data[i] = i;
    for (size_t i = 0; i < size * 5; i++) data[(i * 1009) % size] += 1;
    free((void*)data);
    return 0;
}
EOF

# Compile and move mcf
gcc -O3 ~/cloud2/mcf_dummy.c -o ~/cloud2/mcf
sudo mv ~/cloud2/mcf /usr/local/bin/mcf
touch ~/cloud2/inp.in
```

### Setup Python Environment
```bash
cd ~/cloud2
python3 -m venv telemetry_env
source telemetry_env/bin/activate
pip install flask flask-socketio "python-socketio[client]" scikit-learn pandas numpy joblib
```

---

## 2. Training the Machine Learning Model

### Step 2.1: Run the Experiment Matrix (Data Collection)
Collect training data across 4 victims and 4 attackers (10 runs each = 160 experiments). This takes ~70-90 minutes.
```bash
sudo bash run_experiments.sh
```

### Step 2.2: Train the Model
Train the Random Forest model based on the generated CSV files.
```bash
python3 train_model.py
```
*Expected Output: Model F1 Score should be between 0.95 and 0.99 with no label leakage. Model saves as `rf_model.pkl`.*

---

## 3. Live Demonstration (Closed-Loop Defense)

To run the live demo, open 4 separate terminal windows (or use `tmux`/`screen`).

### Terminal 1: Start Web Dashboard
```bash
cd ~/cloud2
python3 app.py
```
*Open your browser and navigate to `http://<your-server-ip>:5000` to view the dashboard.*

### Terminal 2: Start the Live Defense Daemon
```bash
cd ~/cloud2
sudo python3 live_daemon.py
```
*This daemon continuously monitors the system. If it detects an attack via the ML model, it dynamically isolates the aggressor core using Intel CAT.*

### Terminal 3 & 4: The "Brutal Combo" Test
To trigger a guaranteed Noisy-Neighbor event, run a high-IPC innocent workload alongside a heavy memory bandwidth attacker.

**Terminal 3 (Innocent Victim):**
```bash
while true; do numactl --physcpubind=0 redis-benchmark -t set,get -n 100000000 -q > /dev/null 2>&1; done
```

**Terminal 4 (Aggressor):**
```bash
while true; do numactl --physcpubind=1 ~/cloud2/STREAM/stream_c.exe > /dev/null 2>&1; done
```

### Expected Behavior on Dashboard
1. The **Victim IPC** (green chart) will crash instantly.
2. The **Memory Bandwidth** (blue chart) and **LLC Misses** (yellow chart) will skyrocket.
3. The **ML Confidence** (purple chart) will jump to 90-100%.
4. After 3 seconds, the Daemon triggers **Mitigation** (CAT isolation on Core 1).
5. The Victim IPC will recover automatically (Recovery % is logged in Terminal 2 and Dashboard Event Log).

---

## Note on ML Model Intelligence (The MCF scenario)
If you run `mcf` as the victim instead of `redis-benchmark`, the ML Confidence will stay at **0%** despite massive Cache Misses and MB/s. This is **intentional**. The ML model learned that `mcf` has a naturally low baseline IPC. Since its IPC does not drop further, the model correctly identifies that no performance degradation is happening to the victim, proving the AI does not just trigger on high RAM usage, but on actual victim starvation.
```
