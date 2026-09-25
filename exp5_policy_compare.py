#!/usr/bin/env python3
"""
Exp 5 — Does ANY mitigation policy actually recover the victim? (decisive test)
==============================================================================
Our continuous time-series (exp3) showed the 'strangulate' policy (CAT 0x003 +
MBA 20%) gives ~0% sustained recovery — the earlier "+81.6%" was very likely
phase-variance. Before we conclude "mitigation doesn't work", we must rule out
that 'strangulate' is simply a BAD policy: restricting the aggressor's cache
(0x003) can backfire for STREAM (more LLC misses -> MORE bandwidth demand).

This runs ONE controlled, continuous experiment under a fixed 5x STREAM attack
and compares three policies back-to-back, each bracketed by a no-mitigation
(reset) window, while logging victim IPC and system memory bandwidth every
200 ms. It then prints a verdict table: median IPC per phase and the recovery
vs. the adjacent attacked window.

Policies compared (victim always protected with COS2 = 0x7FC + MBA 100%):
  strangulate  : aggressors CAT 0x003 + MBA 20%   (current; cache+bw)
  mba20_nocat  : aggressors CAT 0x7FF + MBA 20%   (bandwidth throttle ONLY, no cache cut)
  mba10_nocat  : aggressors CAT 0x7FF + MBA 10%   (hardest bandwidth throttle, MBA min)

If any policy lifts victim IPC toward the ~0.22 baseline and SUSTAINS it -> real,
honest positive result (+ the insight that policy choice matters). If none do ->
bandwidth throttling cannot reserve bandwidth for the victim on this 2-channel
node -> honest negative, and the Verifier is what caught it.

Run as root (perf + pqos + taskset):
    sudo python3 exp5_policy_compare.py | tee exp5_verdict.txt
Send back:  exp5_verdict.txt, exp5_ipc.csv, exp5_bw.csv
"""
import subprocess, os, time, signal, statistics as st

# ----------------------- CONFIG (adjust binary paths) -----------------------
VICTIM_CORE = 0
AGG_CORES   = [1, 2, 3, 4, 5]
MCF         = "/usr/local/bin/mcf"
STREAM      = "/home/lenovo/cloud2/STREAM/stream_c.exe"     # <-- ADJUST IF DIFFERENT
INTERVAL_MS = 200
SETTLE      = 1.5                          # seconds skipped after each transition
IMC_EVENTS  = ",".join(
    f"uncore_imc_{i}/cas_count_read/,uncore_imc_{i}/cas_count_write/" for i in range(6)
)
# schedule: (label, llc_mask, mba_pct, duration_s)  — None mask => reset (no mitigation)
SCHED = [
    ("baseline_alone", "ALONE", None, 6),   # victim alone, aggressors not yet started
    ("attacked",        None,   None, 8),
    ("strangulate",    "0x003", 20,   10),
    ("attacked",        None,   None, 8),
    ("mba20_nocat",    "0x7FF", 20,   10),
    ("attacked",        None,   None, 8),
    ("mba10_nocat",    "0x7FF", 10,   10),
    ("attacked",        None,   None, 8),
]
# ---------------------------------------------------------------------------


def spawn_loop(core, binary):
    return subprocess.Popen(
        ["taskset", "-c", str(core), "bash", "-c",
         f"while true; do {binary} >/dev/null 2>&1; done"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )


def kill_tree(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        pass


def pq(*args):
    subprocess.run(["sudo", "pqos", "-I", *args],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_policy(mask, mba):
    pq("-e", f"llc:1={mask}")
    pq("-e", f"mba:1={mba}")
    pq("-e", "llc:2=0x7FC")          # victim protected ways
    pq("-e", "mba:2=100")
    pq("-a", "llc:1=" + ",".join(map(str, AGG_CORES)))
    pq("-a", f"llc:2={VICTIM_CORE}")


def reset_pqos():
    pq("-R")


def to_bytes(val, unit):
    u = unit.strip()
    if u in ("GiB", "GB"):  return val * 1073741824
    if u in ("MiB", "MB"):  return val * 1048576
    if u in ("KiB", "KB"):  return val * 1024
    if u in ("B", "Bytes"): return val
    return val * 64


def load_interval(path):
    """perf -I -x , -> {t: {event: value}}"""
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
        out.setdefault(round(t, 3), {})[p[3]] = (v, p[2])
    return out


def main():
    victim = spawn_loop(VICTIM_CORE, MCF)
    time.sleep(2)

    total = sum(d for *_, d in SCHED)
    ipc_perf = subprocess.Popen(
        ["perf", "stat", "-I", str(INTERVAL_MS), "-x", ",",
         "-e", "instructions,cycles", "-C", str(VICTIM_CORE),
         "-o", "exp5_ipc.csv", "--", "sleep", str(total)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    bw_perf = subprocess.Popen(
        ["perf", "stat", "-I", str(INTERVAL_MS), "-x", ",",
         "-e", IMC_EVENTS, "-a", "-o", "exp5_bw.csv", "--", "sleep", str(total)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)

    t0 = time.time()
    marks, aggs = [], []

    def at(sec):
        while time.time() - t0 < sec:
            time.sleep(0.03)

    elapsed = 0.0
    for label, mask, mba, dur in SCHED:
        start = elapsed
        if label == "baseline_alone":
            pass                                  # aggressors not started yet
        elif mask == "ALONE":
            pass
        else:
            if label == "attacked":
                reset_pqos()
            else:
                apply_policy(mask, mba)
        # launch aggressors right when baseline_alone ends
        if label == "baseline_alone":
            at(start + dur)
            print(f"[{start+dur:.0f}s] launching {len(AGG_CORES)}x STREAM")
            aggs = [spawn_loop(c, STREAM) for c in AGG_CORES]
            marks.append((label, start, start + dur))
            elapsed += dur
            continue
        print(f"[{start:.0f}s] phase: {label}" + (f" (CAT {mask}, MBA {mba}%)" if mask and mask != 'ALONE' else ""))
        at(start + dur)
        marks.append((label, start, start + dur))
        elapsed += dur

    ipc_perf.wait(); bw_perf.wait()
    reset_pqos()
    for p in aggs:
        kill_tree(p)
    kill_tree(victim)
    subprocess.run(["pkill", "-f", MCF], stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-f", STREAM], stderr=subprocess.DEVNULL)

    # ---------------- analyze ----------------
    ipc_raw = load_interval("exp5_ipc.csv")
    bw_raw = load_interval("exp5_bw.csv")
    ipc_ts = []
    for t, d in sorted(ipc_raw.items()):
        if "instructions" in d and "cycles" in d and d["cycles"][0] > 0:
            ipc_ts.append((t, d["instructions"][0] / d["cycles"][0]))
    bw_ts = []
    for t, d in sorted(bw_raw.items()):
        casb = sum(to_bytes(v, u) for ev, (v, u) in d.items() if "cas_count" in ev)
        bw_ts.append((t, casb / 0.2 / 1e9))       # GB/s over the 200 ms window

    def med(ts, a, b):
        xs = [v for t, v in ts if a + SETTLE <= t < b]
        return st.median(xs) if xs else float("nan")

    print("\n================= VERDICT =================")
    print(f"{'phase':16s} {'IPC(med)':>9s} {'BW(med GB/s)':>13s} {'recovery vs attacked':>22s}")
    base = None
    last_attacked_ipc = None
    for label, a, b in marks:
        ipc = med(ipc_ts, a, b)
        bw = med(bw_ts, a, b)
        rec = ""
        if label == "baseline_alone":
            base = ipc
        elif label == "attacked":
            last_attacked_ipc = ipc
        else:
            if last_attacked_ipc:
                rec = f"{(ipc-last_attacked_ipc)/last_attacked_ipc*100:+.1f}%"
        print(f"{label:16s} {ipc:9.3f} {bw:13.1f} {rec:>22s}")
    if base:
        print(f"\nbaseline (alone) IPC = {base:.3f}.  A policy 'works' only if its IPC")
        print(f"climbs toward {base:.3f} AND stays there (not a one-off spike).")
    print("Send: exp5_verdict.txt, exp5_ipc.csv, exp5_bw.csv")


if __name__ == "__main__":
    main()
