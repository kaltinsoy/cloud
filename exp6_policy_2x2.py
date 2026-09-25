#!/usr/bin/env python3
"""
Exp 6 — Complete the cache x MBA 2x2, and measure the mechanism directly.
=========================================================================
A reviewer correctly noted that exp5 cannot support "cache restriction is
counterproductive": the only cache-isolated comparison (at MBA 20%) is
0x003 -> 61% vs 0x7FF -> 59%, i.e. restricting cache is NOT harmful, and the
78% win comes purely from MBA 20% -> 10% (cache held at 0x7FF). The missing
cell is strangulate-hard (0x003 + MBA 10%).

This experiment:
  (1) Fills the 2x2 by adding strangulate-hard (0x003 + MBA 10%), so the cache
      effect can be isolated at BOTH MBA levels.
  (2) Measures the proposed MECHANISM directly: per-core LLC miss rate of an
      aggressor (core 1), to test whether restricting its cache to two ways
      actually raises its miss rate / memory traffic.

Same controlled within-run alternation as exp5 (each policy bracketed by a
no-mitigation window), three runs recommended. Victim MemBench on core 0;
5x STREAM aggressors on cores 1-5; victim protected with CAT 0x7FC + MBA 100%.

Run as root:  sudo python3 exp6_policy_2x2.py | tee exp6_verdict.txt
Send back:  exp6_verdict.txt, exp6_ipc.csv, exp6_bw.csv, exp6_aggr.csv
"""
import subprocess, os, time, signal, statistics as st

# ----------------------- CONFIG (adjust binary paths) -----------------------
VICTIM_CORE = 0
AGG_CORES   = [1, 2, 3, 4, 5]
AGGR_PROBE  = 1                         # one aggressor core we instrument for the mechanism
MCF         = "/usr/local/bin/mcf"
STREAM      = "/home/lenovo/cloud2/STREAM/stream_c.exe"   # <-- ADJUST IF DIFFERENT
INTERVAL_MS = 200
SETTLE      = 1.5
IMC_EVENTS  = ",".join(
    f"uncore_imc_{i}/cas_count_read/,uncore_imc_{i}/cas_count_write/" for i in range(6))
# schedule: (label, llc_mask, mba_pct, dur) ; None mask => no mitigation (reset)
SCHED = [
    ("baseline_alone",  "ALONE", None, 6),
    ("attacked",         None,   None, 8),
    ("strangulate",     "0x003", 20,   10),   # cache-restricted, moderate MBA
    ("attacked",         None,   None, 8),
    ("mba20_nocat",     "0x7FF", 20,   10),   # cache-free,       moderate MBA
    ("attacked",         None,   None, 8),
    ("strangulate_hard","0x003", 10,   10),   # cache-restricted, HARD MBA  <-- MISSING CELL
    ("attacked",         None,   None, 8),
    ("mba10_nocat",     "0x7FF", 10,   10),   # cache-free,       HARD MBA
    ("attacked",         None,   None, 8),
]
# ---------------------------------------------------------------------------


def spawn_loop(core, binary):
    return subprocess.Popen(
        ["taskset", "-c", str(core), "bash", "-c",
         f"while true; do {binary} >/dev/null 2>&1; done"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)


def kill_tree(p):
    try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception: pass


def pq(*a):
    subprocess.run(["sudo", "pqos", "-I", *a],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_policy(mask, mba):
    pq("-e", f"llc:1={mask}"); pq("-e", f"mba:1={mba}")
    pq("-e", "llc:2=0x7FC");   pq("-e", "mba:2=100")
    pq("-a", "llc:1=" + ",".join(map(str, AGG_CORES)))
    pq("-a", f"llc:2={VICTIM_CORE}")


def reset_pqos(): pq("-R")


def to_bytes(v, u):
    u = u.strip()
    if u in ("GiB", "GB"): return v * 1073741824
    if u in ("MiB", "MB"): return v * 1048576
    if u in ("KiB", "KB"): return v * 1024
    if u in ("B", "Bytes"): return v
    return v * 64


def load(path):
    out = {}
    for line in open(path):
        if line.startswith("#") or not line.strip(): continue
        p = [x.strip() for x in line.split(",")]
        if len(p) < 4: continue
        try: t = float(p[0]); v = float(p[1])
        except ValueError: continue
        out.setdefault(round(t, 3), {})[p[3]] = (v, p[2])
    return out


def main():
    victim = spawn_loop(VICTIM_CORE, MCF); time.sleep(2)
    total = sum(d for *_, d in SCHED)
    ipc_perf = subprocess.Popen(["perf","stat","-I",str(INTERVAL_MS),"-x",",",
        "-e","instructions,cycles","-C",str(VICTIM_CORE),"-o","exp6_ipc.csv","--","sleep",str(total)],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,start_new_session=True)
    bw_perf = subprocess.Popen(["perf","stat","-I",str(INTERVAL_MS),"-x",",",
        "-e",IMC_EVENTS,"-a","-o","exp6_bw.csv","--","sleep",str(total)],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,start_new_session=True)
    aggr_perf = subprocess.Popen(["perf","stat","-I",str(INTERVAL_MS),"-x",",",
        "-e","instructions,cycles,LLC-loads,LLC-load-misses","-C",str(AGGR_PROBE),
        "-o","exp6_aggr.csv","--","sleep",str(total)],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,start_new_session=True)

    t0 = time.time(); marks, aggs = [], []
    def at(s):
        while time.time() - t0 < s: time.sleep(0.03)

    elapsed = 0.0
    for label, mask, mba, dur in SCHED:
        start = elapsed
        if label == "baseline_alone":
            at(start + dur)
            print(f"[{start+dur:.0f}s] launching {len(AGG_CORES)}x STREAM")
            aggs = [spawn_loop(c, STREAM) for c in AGG_CORES]
            marks.append((label, start, start + dur)); elapsed += dur; continue
        if label == "attacked": reset_pqos()
        else: apply_policy(mask, mba)
        print(f"[{start:.0f}s] {label}" + (f" (CAT {mask}, MBA {mba}%)" if mask and mask != 'ALONE' else ""))
        at(start + dur); marks.append((label, start, start + dur)); elapsed += dur

    ipc_perf.wait(); bw_perf.wait(); aggr_perf.wait()
    reset_pqos()
    for p in aggs: kill_tree(p)
    kill_tree(victim)
    subprocess.run(["pkill","-f",MCF],stderr=subprocess.DEVNULL)
    subprocess.run(["pkill","-f",STREAM],stderr=subprocess.DEVNULL)

    ipc = load("exp6_ipc.csv"); bw = load("exp6_bw.csv"); ag = load("exp6_aggr.csv")
    ipc_ts = [(t, d["instructions"][0]/d["cycles"][0]) for t, d in sorted(ipc.items())
              if "instructions" in d and "cycles" in d and d["cycles"][0] > 0]
    bw_ts = [(t, sum(to_bytes(v,u) for ev,(v,u) in d.items() if "cas_count" in ev)/0.2/1e9)
             for t, d in sorted(bw.items())]
    miss_ts = [(t, d["LLC-load-misses"][0]/d["LLC-loads"][0]) for t, d in sorted(ag.items())
               if "LLC-loads" in d and "LLC-load-misses" in d and d["LLC-loads"][0] > 0]

    def med(ts, a, b):
        xs = [v for t, v in ts if a + SETTLE <= t < b]
        return st.median(xs) if xs else float("nan")

    base = None; res = {}
    print("\n===== 2x2 VERDICT  (cache x MBA) =====")
    print(f"{'phase':18s} {'victim%base':>11s} {'sysBW':>7s} {'aggr_missrate':>14s}")
    for label, a, b in marks:
        ipcm = med(ipc_ts, a, b); bwm = med(bw_ts, a, b); mr = med(miss_ts, a, b)
        if label == "baseline_alone": base = ipcm
        pct = (ipcm / base * 100) if base else float("nan")
        res[label] = (pct, bwm, mr)
        print(f"{label:18s} {pct:10.1f}% {bwm:7.1f} {mr:14.3f}")

    def g(k): return res.get(k, (float('nan'),)*3)
    print("\n--- CACHE EFFECT, MBA held fixed (this is the test) ---")
    print(f"  @MBA20:  0x003={g('strangulate')[0]:.1f}%  vs  0x7FF={g('mba20_nocat')[0]:.1f}%   "
          f"(cache delta {g('strangulate')[0]-g('mba20_nocat')[0]:+.1f} pts)")
    print(f"  @MBA10:  0x003={g('strangulate_hard')[0]:.1f}%  vs  0x7FF={g('mba10_nocat')[0]:.1f}%   "
          f"(cache delta {g('strangulate_hard')[0]-g('mba10_nocat')[0]:+.1f} pts)")
    print("--- MECHANISM: does 0x003 raise the aggressor's LLC miss rate? ---")
    print(f"  @MBA20:  0x003 miss={g('strangulate')[2]:.3f}  vs  0x7FF miss={g('mba20_nocat')[2]:.3f}")
    print(f"  @MBA10:  0x003 miss={g('strangulate_hard')[2]:.3f}  vs  0x7FF miss={g('mba10_nocat')[2]:.3f}")
    print("\nIf cache delta is ~0/positive at both MBA -> 'cache restriction is neutral, MBA is the lever'.")
    print("If 0x003 clearly worse AND raises aggressor miss rate -> the 'cache backfires' claim is supported.")
    print("Send: exp6_verdict.txt, exp6_ipc.csv, exp6_bw.csv, exp6_aggr.csv")


if __name__ == "__main__":
    main()
