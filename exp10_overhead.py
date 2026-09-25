#!/usr/bin/env python3
"""
Exp 10 — Control-plane overhead of Temenos (reviewer Q5).
=========================================================
Measures three costs of the live loop, honestly and directly:
  1. RDT actuation latency: apply strangulate to cores 1-5 + verify binding (ms).
  2. Detector inference latency: one RandomForest predict on a 1-row frame (ms).
  3. Telemetry cost: CPU time consumed by the three concurrent 1 s perf
     samplers, per sample (i.e., overhead of observation itself).

Run as root FROM THE DAEMON DIRECTORY (~/cloud2 — needs mitigation_shield.py
and rf_model.pkl next to it), takes ~1 minute:
    sudo python3 exp10_overhead.py | tee exp10_overhead.txt
Send back: exp10_overhead.txt
"""
import time, resource, subprocess, statistics as st

import mitigation_shield as m

VICTIM_CORE = 0
AGG_CORES = [1, 2, 3, 4, 5]
UNCORE = ",".join(f"uncore_imc_{i}/cas_count_read/,uncore_imc_{i}/cas_count_write/" for i in range(6))

# ---------- 1) actuation latency ----------
lat = []
ok_all = True
for _ in range(10):
    m.reset_all(); time.sleep(0.3)
    t0 = time.perf_counter()
    applied = m.apply_policy_cores(AGG_CORES, "strangulate", victim_core=VICTIM_CORE)
    ok = m.verify_association(AGG_CORES[0], m.ATTACKER_COS)
    lat.append((time.perf_counter() - t0) * 1000)
    ok_all = ok_all and bool(applied) and (ok is True)
m.reset_all()
print(f"[1] RDT actuation+verify latency: median {st.median(lat):.1f} ms "
      f"(min {min(lat):.1f}, max {max(lat):.1f}, n=10, binding_ok={ok_all})")

# ---------- 2) inference latency ----------
import joblib, pandas as pd
rf = joblib.load("rf_model.pkl")
X = pd.DataFrame([{ "IPC": 0.5, "Total_MB_per_sec": 8000.0, "cache_dominance_score": 0.02,
                    "LLC-load-misses": 50000.0, "LLC-loads": 200000.0 }])
for _ in range(10):
    rf.predict(X)                      # warm-up
tt = []
for _ in range(200):
    t0 = time.perf_counter(); rf.predict(X); tt.append((time.perf_counter() - t0) * 1000)
print(f"[2] RF inference latency: median {st.median(tt):.2f} ms per window (n=200)")

# ---------- 3) telemetry CPU cost per 1 s sample ----------
def observe_once():
    cmds = [
        ["perf", "stat", "-x", ",", "-C", str(VICTIM_CORE),
         "-e", "cycles,instructions,LLC-loads,LLC-load-misses", "sleep", "1"],
        ["perf", "stat", "-x", ",", "-C", str(AGG_CORES[0]),
         "-e", "cycles,instructions,LLC-loads,LLC-load-misses", "sleep", "1"],
        ["perf", "stat", "-x", ",", "-a", "-e", UNCORE, "sleep", "1"],
    ]
    ps = [subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) for c in cmds]
    for p in ps:
        p.communicate()

N = 10
r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
w0 = time.perf_counter()
for _ in range(N):
    observe_once()
w1 = time.perf_counter()
r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
print(f"[3] Telemetry: {cpu/N*1000:.1f} ms CPU per 1 s sample "
      f"({cpu/N*100:.2f}% of one core); wall {(w1-w0)/N:.3f} s per sample (n={N})")

print("\nDerived: detection-to-enforcement = K x sampling + actuation "
      f"= 3 x 1 s + {st.median(lat)/1000:.2f} s ≈ {3 + st.median(lat)/1000:.1f} s")
print("Send: exp10_overhead.txt")
