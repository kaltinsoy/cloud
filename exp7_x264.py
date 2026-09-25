#!/usr/bin/env python3
"""
Exp 7 — x264 as a REAL (compute-bound) victim: is it harmed, and does mitigation help?
=====================================================================================
So far x264 is only in the detector's training matrix; we never ran it through the
mitigation/Verifier loop. Adding it gives a second, non-synthetic victim (beyond
MemBench) and tests the thesis on a real workload.

x264 is compute/SIMD-bound, so under a bandwidth attack we EXPECT little harm and an
INEFFECTIVE verdict — which is exactly "mitigation benefit is conditional on the victim
being bandwidth-bound." We report whatever actually comes out; nothing is assumed.

Same controlled within-run protocol as exp5/exp6: x264 on core 0, 5x STREAM on cores 1-5,
victim protected (CAT 0x7FC + MBA 100%); alternate no-mitigation and the best policy from
exp5/exp6 (MBA-only hard: CAT 0x7FF + MBA 10%), sampling x264's IPC every 200 ms.

Run as root from the dir with mitigation_shield.py:
    sudo python3 exp7_x264.py | tee exp7_verdict.txt
Run it 2-3 times (exp7_verdict.txt, _2, _3). Send back: exp7_verdict*.txt, exp7_ipc.csv
"""
import subprocess, os, time, signal, statistics as st

# ----------------------- CONFIG (adjust the two marked lines) -----------------------
VICTIM_CORE = 0
AGG_CORES   = [1, 2, 3, 4, 5]
# >>> SET THIS to YOUR x264 victim invocation (the one you used to build the training
#     data). It must keep core 0 busy for several seconds per encode; the loop restarts
#     it if it finishes, so use a long enough input / high --frames.
VICTIM_CMD  = "x264 --threads 1 --quiet --frames 20000 -o /dev/null /path/to/input.y4m"   # <-- ADJUST
STREAM      = "/home/lenovo/cloud2/STREAM/stream_c.exe"   # <-- ADJUST
INTERVAL_MS = 200
SETTLE      = 1.5
POL_MASK, POL_MBA = "0x7FF", 10          # best policy from exp5/exp6 (hard MBA-only)
SCHED = [
    ("baseline_alone", "ALONE", None, 8),    # x264 solo
    ("attacked",        None,   None, 10),    # + 5x STREAM, no mitigation
    ("mitigated",      POL_MASK, POL_MBA, 10),
    ("attacked",        None,   None, 8),
    ("mitigated",      POL_MASK, POL_MBA, 10),
    ("attacked",        None,   None, 8),
]
# -----------------------------------------------------------------------------------


def spawn(core, cmd):
    return subprocess.Popen(
        ["taskset", "-c", str(core), "bash", "-c", f"while true; do {cmd} >/dev/null 2>&1; done"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)


def kill_tree(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        pass


def pq(*a):
    subprocess.run(["sudo", "pqos", "-I", *a], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_policy(mask, mba):
    pq("-e", f"llc:1={mask}"); pq("-e", f"mba:1={mba}")
    pq("-e", "llc:2=0x7FC");   pq("-e", "mba:2=100")
    pq("-a", "llc:1=" + ",".join(map(str, AGG_CORES)))
    pq("-a", f"llc:2={VICTIM_CORE}")


def reset_pqos(): pq("-R")


def load(path):
    out = {}
    for line in open(path):
        if line.startswith("#") or not line.strip():
            continue
        p = [x.strip() for x in line.split(",")]
        if len(p) < 4:
            continue
        try:
            t = float(p[0]); v = float(p[1])
        except ValueError:
            continue
        out.setdefault(round(t, 3), {})[p[3]] = v
    return out


def main():
    victim = spawn(VICTIM_CORE, VICTIM_CMD)
    time.sleep(3)
    total = sum(d for *_, d in SCHED)
    perf = subprocess.Popen(
        ["perf", "stat", "-I", str(INTERVAL_MS), "-x", ",",
         "-e", "instructions,cycles", "-C", str(VICTIM_CORE),
         "-o", "exp7_ipc.csv", "--", "sleep", str(total)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)

    t0 = time.time(); marks, aggs = [], []
    def at(s):
        while time.time() - t0 < s:
            time.sleep(0.03)

    elapsed = 0.0
    for label, mask, mba, dur in SCHED:
        start = elapsed
        if label == "baseline_alone":
            at(start + dur)
            print(f"[{start+dur:.0f}s] launching {len(AGG_CORES)}x STREAM")
            aggs = [spawn(c, STREAM) for c in AGG_CORES]
            marks.append((label, start, start + dur)); elapsed += dur; continue
        if label == "attacked":
            reset_pqos()
        else:
            apply_policy(mask, mba)
        print(f"[{start:.0f}s] {label}")
        at(start + dur); marks.append((label, start, start + dur)); elapsed += dur

    perf.wait(); reset_pqos()
    for p in aggs:
        kill_tree(p)
    kill_tree(victim)
    subprocess.run(["pkill", "-f", "x264"], stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-f", STREAM], stderr=subprocess.DEVNULL)

    ipc = load("exp7_ipc.csv")
    ts = [(t, d["instructions"] / d["cycles"]) for t, d in sorted(ipc.items())
          if "instructions" in d and "cycles" in d and d["cycles"] > 0]

    def med(a, b):
        xs = [v for t, v in ts if a + SETTLE <= t < b]
        return st.median(xs) if xs else float("nan")

    base = None; att, mit = [], []
    print("\n===== x264 VERDICT =====")
    print(f"{'phase':16s} {'IPC(med)':>9s} {'%baseline':>10s}")
    for label, a, b in marks:
        m = med(a, b)
        if label == "baseline_alone":
            base = m
        pct = (m / base * 100) if base else float("nan")
        print(f"{label:16s} {m:9.3f} {pct:9.1f}%")
        if label == "attacked":  att.append(m)
        if label == "mitigated": mit.append(m)

    attm = st.median(att) if att else float("nan")
    mitm = st.median(mit) if mit else float("nan")
    rec = (mitm - attm) / attm * 100 if attm else 0.0
    verdict = "effective" if rec >= 30 else ("partial" if rec >= 10 else "ineffective")
    print(f"\nbaseline IPC  = {base:.3f}")
    print(f"attacked IPC  = {attm:.3f}  ({attm/base*100:.1f}% of baseline)")
    print(f"mitigated IPC = {mitm:.3f}")
    print(f"recovery vs attacked = {rec:+.1f}%   ->  VERDICT: {verdict.upper()}")
    print("\n(If attacked stays near baseline and recovery is small, x264 is compute-bound,")
    print(" barely harmed -> INEFFECTIVE, which confirms the conditional-benefit thesis on a real victim.)")
    print("Send: exp7_verdict*.txt, exp7_ipc.csv")


if __name__ == "__main__":
    main()
