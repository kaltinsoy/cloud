# Temenos: A Self-Validating Closed-Loop System for Noisy-Neighbor Mitigation in Multi-Tenant Servers

Artifact repository (dataset, scripts, and raw results) for the paper accepted at **IEEE CloudCom 2026** (paper #53).

**Authors:** Anıl Koray Altınsoy¹, Kazım Önses¹, Reza Zare Hassanpour², Kasım Öztoprak¹
¹ Department of Computer Engineering, Konya Food and Agriculture University, Konya, Türkiye
² Computer Science Department, University of Groningen, Groningen, Netherlands

Temenos detects noisy-neighbor interference from commodity hardware counters, throttles the aggressors with Intel RDT (CAT + MBA), and **measures** whether the victim actually recovered instead of assuming it did.

---

## How the loop works

1. **Telemetry.** Linux `perf` reads per-core PMU counters (cycles, instructions, LLC-loads, LLC-load-misses) and uncore IMC `cas_count_read/write` counters. Training data is sampled every 100 ms; the online controller uses 1 s windows.
2. **Detection.** A Random Forest (100 trees, `max_depth=12`, `class_weight='balanced'`) over five features: IPC, memory bandwidth, cache-dominance score (LLC hits per cycle), LLC-load-misses, LLC-loads.
3. **Hysteresis.** Mitigation triggers after K=3 consecutive positive windows and is released after M=10 consecutive clean ones.
4. **Mitigation.** `pqos`/resctrl binds the aggressor cores to a restricted class of service (default *strangulate*: CAT `0x003` + MBA 20%) and the victim to a protected one (CAT `0x7FC` + MBA 100%). The binding of an aggressor core is read back with `pqos -s`.
5. **Verifier.** Mean victim IPC over N=7 one-second windows before actuation and N after a 3 s settling delay. Recovery is labeled effective (≥30%), partial (10–30%), or ineffective (<10%).

**Current prototype, as stated in the paper:** the Verifier's verdict is logged but does not drive release, which stays detector/hysteresis-driven. The localizer (`find_aggressor_core`) scores the monitored cores and logs the dominant one, but actuation uses the configured core list (`ATTACKER_CORES`).

## Main results (from the paper)

| What | Result |
|---|---|
| Detector (leakage-aware group split) | F1 = 0.9991. Labels coincide with aggressor presence, so read this as a sanity check. |
| Intensity | One STREAM aggressor is harmless (5 of 6 live verdicts ineffective). The knee is at 3 aggressors. A sustained 5-aggressor attack starves MemBench to ≈30% of baseline. |
| Cache × bandwidth factorial (median of 3 runs) | strangulate 61.5%, MBA-only 59.6%, strangulate-hard (0x003 + MBA 10%) **81.2%**, MBA-only-hard 79.6%. MBA strength is the lever; cache fencing adds about 2 points. |
| x264 (one run) | 84% → 91% of solo IPC (+8.3%) |
| Redis, pinned (one run per condition) | p99 latency 0.45 → 0.54 ms under attack (+20%); strangulate brings it to 0.43 ms. Throughput 78.1k → 70.9k → 77.0k req/s. |
| Overhead | Telemetry costs 6.8% of one core. Inference takes 18 ms. RDT actuation takes 105 ms (median). First detection to COS binding takes ≈10.5 s, dominated by the K + N sampling windows. |

## Testbed

- Dual Intel Xeon Gold 6136 (Skylake-SP). All experiments run on NUMA node 0: 12 cores, 24.75 MB L3 (11-way CAT, `cbm_mask 0x7ff`), and 2 populated DDR4-2666 channels.
- Ubuntu 24.04.4 LTS with kernel 6.8.0-124, Linux perf 6.8.12, intel-cmt-cat (`pqos`) 23.11.1, Python 3.12, and scikit-learn 1.8.0.
- Skylake-SP's CMT/MBM monitoring is unreliable (erratum), so bandwidth is read from the uncore IMC counters. Linux may disable L3 CAT by default on Skylake-SP. We booted with `rdt=l3cat,mba`.

## Repository map

**Pipeline**

| File | Role |
|---|---|
| `telemetry.py` | Collects per-core and uncore counters for one training run and aligns the streams on a 100 ms timestamp |
| `feature_engineering.py` | Rolling features and the aggressor-scoring heuristic (`find_aggressor_core`) |
| `train_model.py` | Multi-signal labeling and training of the deployed Random Forest (`rf_model.pkl`, `baseline_ipc.pkl`) |
| `live_daemon2.py` | Online loop (telemetry → RF → hysteresis → mitigation → Verifier) used for the live Verifier runs. `live_daemon.py` is the earlier single-core version. |
| `mitigation_shield.py` | `pqos` wrapper for the CAT/MBA policies, with binding verification |
| `mitigation_verification.py` | Verifier (before/after IPC, verdict thresholds) |
| `app.py`, `templates/index.html` | Optional Flask/Socket.IO dashboard |
| `run_scenarios*.sh`, `collect_results.py` | Live Verifier scenarios and result collection |

**Workloads**

- `mcf_dummy.c` is **MemBench**, the memory-bound victim. It is a small custom kernel that does read-modify-write updates at a prime stride of 1009 over a 256 MiB array. It stands in for the proprietary SPEC mcf and is **not** SPEC mcf. `inp.in` is an empty placeholder argument.
- External tools: STREAM, stress-ng, Redis / redis-benchmark, x264, and sysbench.

**Dataset**

- `data_<victim>_vs_<aggressor>_run<k>.csv` holds the 160 training runs: 4 victims × 4 aggressor conditions × 10 repetitions, 60 s each.
- `master_dataset.csv` holds the 86,720 merged, labeled samples.
- `train_output.txt` is the training log.

**Paper experiments and their raw outputs**

| Script | Paper | Output |
|---|---|---|
| `exp2_sweep.py` | Fig. 2(a): aggressor-count sweep | `sweep.csv` |
| `exp6_policy_2x2.py` | Table II, Fig. 2(b): cache × bandwidth factorial | `exp6_results_{1,2,3}.txt`, `exp6_*.csv` |
| `exp1_redis_tail.sh` | Table III: Redis tail latency | `redis_A_solo.txt`, `redis_B_attacked.txt`, `redis_C_mitigated.txt` |
| `exp7_x264.py` | Sec. VI-F: x264 victim | `exp7_ipc.csv` |
| `exp9_mba_calibration.py` | Sec. VI-D: MBA calibration | `exp9_mba_calibration*.txt` |
| `exp10_overhead.py` | Sec. VI-G: control-plane overhead | `exp10_overhead.txt` |
| `run_scenarios_v2.sh` + `live_daemon2.py` | Sec. VI-C: live single-aggressor verdicts | `paper_results.txt` (n=5 per scenario), `paper_results_v1.txt` (earlier single trigger), `paper_results_*.json` |
| `leakage_eval.py` | Sec. VI-A/B: leakage-aware detector evaluation, feature importance | `leakage_results.txt` |
| `figures/make_fig_results.py`, `figures/make_fig1.py` | Fig. 2 (from `sweep.csv` + `exp6_results_*.txt`), Fig. 1 | `figures/fig_results.png`, `figures/fig1.png` |

The following are exploratory or superseded and not used in the paper's figures or tables: `exp3_timeseries.py` (`ts_raw.csv`), `exp4_blind_cost.py` (`blind_cost.txt`), and `exp5_policy_compare.py` (`exp5_*`, superseded by the full factorial in `exp6`).

`leakage_eval.py` reproduces the leakage-aware evaluation reported in the paper. With a group split by experiment it gives F1 = 0.9991, and group 5-fold CV gives 0.9995 ± 0.0001. `train_model.py` trains the deployed model with random splits.

## Reproducing

All of this needs root, an Intel CPU with RDT CAT/MBA, and uncore IMC perf events. Pin everything to one NUMA node.

```bash
# 1. Check the hardware
sudo pqos -s                          # L3 CAT + MBA must be listed
sudo perf list | grep cas_count       # uncore IMC events
sudo mount -t resctrl resctrl /sys/fs/resctrl 2>/dev/null

# 2. Install dependencies
sudo apt install -y linux-tools-common linux-tools-generic intel-cmt-cat \
     stress-ng redis-server redis-tools sysbench x264 numactl
pip install scikit-learn pandas numpy joblib flask flask-socketio

# 3. Build the workloads
gcc -O3 mcf_dummy.c -o mcf && sudo mv mcf /usr/local/bin/mcf     # MemBench
# STREAM: https://github.com/jeffhammond/STREAM -> gcc -O2 -fopenmp stream.c -o stream

# 4. Collect the training data and train
sudo bash run_experiments.sh          # ~3 h: 160 runs
python3 train_model.py
python3 leakage_eval.py              # leakage-aware evaluation (paper Sec. VI-A)

# 5. Run the live loop (optional dashboard: python3 app.py)
sudo python3 live_daemon2.py

# 6. Run the paper experiments (first edit the STREAM path in each script's CONFIG block)
sudo python3 exp6_policy_2x2.py       # repeat 3x for Table II
sudo python3 exp2_sweep.py
sudo python3 exp9_mba_calibration.py
sudo python3 exp10_overhead.py
sudo python3 exp7_x264.py
bash exp1_redis_tail.sh               # see the note below

# 7. Regenerate the figures from the raw outputs
python3 figures/make_fig_results.py
python3 figures/make_fig1.py
```

**Redis note.** Stop any system Redis first (`sudo systemctl stop redis-server`). Otherwise redis-benchmark talks to that unpinned instance on port 6379 instead of the core-pinned one the script starts. The `host configuration "save"` line in the output must be empty.

If an experiment is interrupted, reset RDT state with `sudo python3 -c "import mitigation_shield as m; m.reset_all()"`.

## Limitations

These are stated in the paper:

- All results come from one Skylake-SP node, one NUMA domain, and two memory channels.
- The mitigation experiments use only STREAM aggressors.
- The victim set is small, and the sweep, the x264 probe, and the Redis probe are single runs.
- The verdict does not yet drive control.
- Aggressor localization has not been evaluated.

## Other files

`GUNCELLEME_NOTU.md` and `MIGRATION_NOTES.md` are earlier development notes, written in Turkish, and kept for history. Where they differ from the paper or this README, the paper and this README are authoritative.

## Citation

```bibtex
@inproceedings{temenos2026cloudcom,
  author    = {Anıl Koray Altınsoy and Kazım Önses and Reza Zare Hassanpour and Kasım Öztoprak},
  title     = {Temenos: A Self-Validating Closed-Loop System for Noisy-Neighbor Mitigation in Multi-Tenant Servers},
  booktitle = {Proc. IEEE International Conference on Cloud Computing Technology and Science (CloudCom)},
  year      = {2026},
  note      = {To appear}
}
```

## Acknowledgment

This work was supported by the Scientific and Technological Research Council of Türkiye (TÜBİTAK) under project no. 1919B022606284, and by the Kapsül Technology Platform (Kapsül Teknoloji Platformu) of the Konya Metropolitan Municipality.
