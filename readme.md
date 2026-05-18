# Project 8: Noisy-Neighbor Detection (Phase 1 Prototype)

**Authors:** Anıl Koray Altınsoy & Kazım Önses
**Course:** Cloud Computing (Level 2)
**Target Hardware:** Intel Xeon Gold 6136 (1st Gen Xeon Scalable, Skylake-SP)

## Overview

This repository contains the Phase 1 closed-loop prototype for our
Project 8 submission. The system continuously monitors CPU performance
counters, detects noisy-neighbor interference using a trained Random
Forest classifier, and automatically applies Intel CAT/MBA cache
partitioning to isolate the aggressor.

A live web dashboard (Flask + Socket.IO) visualizes telemetry, detected
events, and mitigation effectiveness in real time.

---

## SKX114 Erratum Adaptation

Our target hardware is affected by **Intel Erratum SKX114**, which
disables CMT and MBM monitoring on Skylake-SP processors. The Linux
kernel on our machine excludes these features via the `rdt=l3cat,mba`
boot parameter.

We work around this limitation by deriving equivalent signals from:

1. **`perf_event` PMU counters** — `cycles`, `instructions`,
   `LLC-loads`, `LLC-load-misses` (per-core).
2. **Uncore IMC counters** — `cas_count_read`/`cas_count_write` on all
   six DRAM channels, summed and converted to MB/s.

CAT and MBA (the mitigation side) are **unaffected** by SKX114 and
function normally.

This adaptation is presented in the paper as a methodology contribution:
our detection system does not depend on vendor-specific RDT monitoring
extensions, making it portable to hardware where CMT/MBM are unavailable.

---

## What Changed vs. Initial Phase 1 Submission

The initial Phase 1 submission had several issues that have been
addressed in this revision:

| Issue | Fix |
|---|---|
| Label leakage (`attacker != none → label=1`) gave F1 = 1.00 | Now labels by IPC drop > 20% threshold against per-victim baseline |
| Workload matrix limited to 2 victims × 2 attackers × 3 runs (18 total) | Expanded to 4 victims × 4 attackers × 10 runs (target: 160 experiments) |
| No hysteresis — daemon oscillated between mitigation states | K=3 trigger / M=10 release hysteresis filter added |
| Aggressor core hardcoded as `1` | `find_aggressor_core()` scores both cores by LLC/IMC intensity + cache dominance |
| No mitigation verification | `MitigationVerifier` measures before/after IPC and reports recovery percentage |
| Only victim core (core 0) monitored | Telemetry now collects from both core 0 and core 1 separately |
| `dashboard.py` was a copy of `live_daemon.py` with Socket.IO | Merged into single `live_daemon.py` |
| `cache_dominance.py` and `estimate_cache_dominance.py` duplicated | Consolidated into `feature_engineering.py` |
| Awkward 1-second-sampling-divided-by-10 scaling | Daemon now samples directly at 100ms to match training data |
| Dashboard had 3 charts only | Expanded to 4 views: Live, Event Log, Statistics, Inspector |

---

## Expected Model Performance

With the corrected (IPC-drop-based) labeling, we expect F1 in the
0.85–0.95 range. F1 = 1.00 would indicate a label-leakage problem
and is treated as a warning by `train_model.py`.

Class balance after correct labeling:
- ~60–70% normal samples (solo runs + low-interference pairs)
- ~30–40% interference samples (significant IPC drop)

---

## Repository Structure

```
.
├── app.py                       # Flask + Socket.IO backend
├── live_daemon.py               # Main detection + mitigation loop
├── telemetry.py                 # perf data collection (per-core + IMC)
├── train_model.py               # Model training (IPC-drop labeling)
├── feature_engineering.py       # Rolling window features, aggressor detection
├── mitigation_shield.py         # pqos CAT/MBA wrapper
├── mitigation_verification.py   # Before/after IPC comparison
├── run_experiments.sh           # 5×5×10 experiment matrix runner
├── templates/
│   └── index.html               # 4-view dashboard UI
├── master_dataset.csv           # Merged labeled dataset (generated)
├── rf_model.pkl                 # Trained Random Forest (generated)
├── baseline_ipc.pkl             # Per-victim baseline IPCs (generated)
└── data_*_vs_*_run*.csv         # Per-experiment raw telemetry
```

---

## Setup

### 1. Verify Hardware

```bash
# Verify Intel RDT CAT and MBA are present
sudo pqos -s
# Expected: L3CA capabilities + MBA capabilities detected
# (CMT/MBM may be absent — that's the SKX114 workaround context)

# Verify uncore IMC counters
sudo perf list | grep uncore_imc | head -5

# Mount resctrl if not already mounted
sudo mount -t resctrl resctrl /sys/fs/resctrl 2>/dev/null
```

### 2. Install Dependencies

```bash
# System packages
sudo apt install -y linux-tools-common linux-tools-generic \
                    intel-cmt-cat python3 python3-pip python3-venv \
                    sysbench stress-ng redis-tools

# Python environment
python3 -m venv telemetry_env
source telemetry_env/bin/activate
pip install --upgrade pip
pip install flask flask-socketio "python-socketio[client]" \
            scikit-learn pandas numpy joblib
```

### 3. Workloads

- **STREAM** (`STREAM/stream_c.exe`): clone <https://github.com/jeffhammond/STREAM>, build with `gcc -O2 -fopenmp stream.c -o STREAM/stream_c.exe`.
- **sysbench** and **stress-ng**: via apt.
- **mcf** and **x264** (optional): if not installed, `run_experiments.sh` skips them with a warning.

---

## Usage

### Step 1: Collect Training Data (~3 hours for full matrix)

```bash
sudo bash run_experiments.sh
```

This iterates through 4 victims × 4 attackers × 10 runs and saves
`data_*_vs_*_run*.csv` files. Estimated time: 4×4×10×(60+5) ≈ 173 minutes.

### Step 2: Train the Model

```bash
python3 train_model.py
```

Outputs:
- `master_dataset.csv` — merged + labeled dataset
- `rf_model.pkl` — trained Random Forest
- `baseline_ipc.pkl` — per-victim baseline IPCs

The script prints precision, recall, F1, ROC-AUC, confusion matrix,
5-fold CV F1, and feature importance.

### Step 3: Start Dashboard

```bash
# Terminal 1: dashboard server
python3 app.py
# Open browser: http://localhost:5000
```

### Step 4: Start Daemon

```bash
# Terminal 2: detection + mitigation loop
sudo python3 live_daemon.py
```

### Step 5: Run the Demo

```bash
# Terminal 3: launch victim
numactl --physcpubind=0 redis-benchmark -t set,get -n 100000000 -q

# Terminal 4: launch noisy neighbor
numactl --physcpubind=1 stress-ng --matrix 1 --cache 1
```

Watch the dashboard:
1. Memory bandwidth and LLC misses spike.
2. After 3 consecutive samples flagged as attack, the daemon applies CAT isolation to core 1.
3. After 3 seconds of stabilization, the verifier reports IPC recovery.
4. Once stress-ng terminates, after 10 consecutive clean samples the mitigation is automatically released.

---

## Phase 2 Roadmap

- [ ] Generalization study: test trained model on SPEC CPU 2017 (bzip2, gcc, xalancbmk) and PARSEC 3.0 (blackscholes, canneal, dedup).
- [ ] Effectiveness analysis: quantify IPC recovery on 6 curated co-location pairs.
- [ ] Overhead measurement: monitoring daemon CPU consumption under 10-tenant load (target < 1%).
- [ ] Public dataset release on GitHub with documentation.
- [ ] 6-page IEEE/ACM-format paper draft.

---

## Known Limitations

1. **Single-host scope.** Cluster-level orchestration is future work.
2. **Two-core experiments only.** Multi-tenant scaling beyond two
   workloads is left to future iterations.
3. **CMT/MBM unavailable** on this hardware (SKX114). We use indirect
   estimation instead of direct cache occupancy measurement.
4. **Aggressor detection is heuristic.** Without CMT, we infer aggressor
   identity from multi-signal scoring (LLC intensity, IMC bandwidth,
   cache dominance). This is correct but not deterministic.

These limitations are documented honestly in the paper's "Threats to
Validity" section.
