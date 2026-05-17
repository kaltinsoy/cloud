 code Markdown

# Project 8: Noisy-Neighbor Detection (Phase 1 Prototype)

**Authors:** Anıl Koray Altınsoy & Kazım Önses  
**Date:** May 17, 2026 (Phase 1 Deliverable)  
**Live Dashboard:** [https://dashboard.anilkoray.tr](https://dashboard.anilkoray.tr)

## 🚀 Overview
This repository contains our fully working Phase 1 closed-loop prototype. 

As detailed in our SKX114 Adaptation Plan, we encountered a severe hardware bug on the Intel Xeon Gold 6136 processor (**Intel Erratum SKX114**) which broke the native Intel RDT monitoring capabilities (CMT/MBM). To bypass this, we completely rewrote our telemetry layer to use a vendor-neutral approach relying on `perf_event` and `uncore_imc` counters. 

**Result:** The system successfully observes hardware behavior, detects Noisy Neighbors using a Random Forest ML model, automatically isolates the attacker using Intel CAT (Cache Allocation Technology), and streams the metrics to a live web dashboard.

---

## 🛠️ How We Bypassed the SKX114 Erratum
Because we could no longer read L3 Cache Occupancy (CMT) or Memory Bandwidth (MBM) via `pqos`, we engineered equivalent signals:

1. **Uncore IMC Bandwidth:** We pull `cas_count_read` and `cas_count_write` from all 6 memory controllers (`uncore_imc_0` to `uncore_imc_5`), aggregate them, and reverse-engineer the math to derive exact MB/s.
2. **Cache Dominance Score:** Since we can't see exact MB usage in the cache, we calculate an indirect occupancy proxy:
   `cache_dominance_score = (LLC-loads / cycles) * (1 - (LLC-load-misses / LLC-loads))`

Our Random Forest model trained perfectly on these new features, achieving a **1.00 (100%) F1-Score**.
**Feature Importance:**
- `Total_MB_per_sec`: 53.24%
- `LLC-load-misses`: 31.15%
- `LLC-loads`: 10.02%
- `cache_dominance_score`: 4.45%
- `IPC`: 1.14% *(Proving traditional CPU metrics cannot detect cache contention!)*

---

## 📁 Repository Structure

* **`telemetry.py`**: The data collection engine. Spawns dual `perf stat` processes (Core + System-wide Uncore), parses the output, handles unit-reversal, and engineers the features into CSVs.
* **`run_experiments.sh`**: The automation script that loops through our 5 seed workloads vs. attackers to generate the dataset.
* **`train_model.py`**: Merges the CSVs, trains the Random Forest classifier, and exports the brain as `rf_model.pkl`.
* **`mitigation_shield.py`**: The active defense layer. Uses `pqos` to apply CAT/MBA masks (e.g., trapping an attacker in `0x003` cache ways).
* **`live_daemon.py`**: The Master Control Loop. It samples `perf` every 1 second (scaling down to match 100ms ML training data), runs ML inference, automatically calls `mitigation_shield.py` if an attack is detected, and pushes data to the Web UI via WebSockets.
* **`app.py` & `templates/index.html`**: A Flask + Socket.IO web server that draws beautiful real-time Chart.js graphs of our telemetry.

---

## ⚙️ Setup & Installation

### 1. Install Dependencies
```bash
sudo pip install --break-system-packages --ignore-installed flask flask-socketio "python-socketio[client]" scikit-learn pandas numpy

2. Verify resctrl (Intel RDT)

Ensure the resctrl filesystem is mounted so mitigation_shield.py can partition the cache:
code Bash

sudo mount -t resctrl resctrl /sys/fs/resctrl

🧪 How to Run the Demo (Closed-Loop Test)

To see the autonomous mitigation in action, open 3 separate terminal panes.

Terminal 1: Start the Dashboard
code Bash

python3 app.py

(You can view the UI at http://localhost:5000 or https://dashboard.anilkoray.tr)

Terminal 2: Start the AI Daemon
code Bash

sudo python3 live_daemon.py

(It will say "Successfully connected to the Live Dashboard WebSocket!")

Terminal 3: Trigger the Attack
First, launch a safe Victim workload on Core 0:
code Bash

numactl --physcpubind=0 redis-benchmark -t set,get -n 100000000 -q &

Look at the dashboard: The system will report [✓] SYSTEM STABLE.

Next, launch the Noisy Neighbor on Core 1:
code Bash

numactl --physcpubind=1 stress-ng --matrix 1 --cache 1

What happens next?

    The dashboard's Memory Bandwidth and LLC Miss charts will spike instantly.

    live_daemon.py will catch the anomaly and the ML model will flag the interference.

    The dashboard turns RED.

    The daemon automatically triggers mitigation_shield.py, masking Core 1 to the lowest cache ways (0x003).

    You will see the victim's IPC recover and stabilize!

📝 Notes for Phase 2

    The dataset master_dataset.csv is completely up to date with the vendor-neutral metrics.

    For our final paper and presentation, we must emphasize that bypassing SKX114 makes our research vendor-agnostic, strengthening the paper's contribution!
