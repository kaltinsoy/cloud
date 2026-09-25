#!/usr/bin/env python3
"""
Exp 2 — Aggressor-count sweep (0..6 STREAM cores) vs a fixed MemBench victim.
=============================================================================
Produces the saturation "knee" curve the reviewer asked for: victim IPC and
system memory bandwidth as a function of the number of bandwidth aggressors.
NO mitigation here — this characterizes *where* the victim starts to collapse,
turning the paper's "single vs multi" two-point story into a real curve.

Measurement matches the project's telemetry.py exactly:
  CORE_EVENTS = instructions,cycles,LLC-load-misses,LLC-loads   (perf -C victim)
  IMC         = uncore_imc_*/cas_count_{read,write}/            (perf -a, 64 B/CAS)
  IPC = instructions / cycles ;  BW = CAS_bytes / duration

Run as root (needs perf + taskset):
    sudo python3 exp2_sweep.py
Send back:  sweep.csv
"""
import subprocess, os, time, signal, csv

# ----------------- CONFIG (adjust the two binary paths) -----------------
VICTIM_CORE = 0
MAX_AGG     = 6                          # sweep N = 0..MAX_AGG aggressors on cores 1..N
MCF         = "/usr/local/bin/mcf"       # MemBench (memory-bound victim)
STREAM      = "/home/lenovo/cloud2/STREAM/stream_c.exe"    # STREAM binary  <-- ADJUST IF DIFFERENT
DURATION    = 10                         # seconds of perf measurement per point
CORE_EVENTS = "instructions,cycles,LLC-load-misses,LLC-loads"
IMC_EVENTS  = ",".join(
    f"uncore_imc_{i}/cas_count_read/,uncore_imc_{i}/cas_count_write/" for i in range(6)
)
# ------------------------------------------------------------------------


def spawn_loop(core, binary):
    """Pin `binary` to `core` and keep it running continuously."""
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


def perf_measure(scope, events, dur, outfile):
    """scope = ('-C','0')  or  ('-a',)"""
    cmd = ["perf", "stat", "-x", ",", "-e", events, *scope, "--", "sleep", str(dur)]
    with open(outfile, "w") as err:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=err)


def to_bytes(val, unit):
    u = unit.strip()
    if u in ("GiB", "GB"):  return val * 1073741824
    if u in ("MiB", "MB"):  return val * 1048576
    if u in ("KiB", "KB"):  return val * 1024
    if u in ("B", "Bytes"): return val
    return val * 64                       # raw CAS count -> 64 B per transfer


def parse(path):
    """perf -x , one-shot output -> {event: summed value (bytes for cas_count)}."""
    out = {}
    for line in open(path):
        if line.startswith("#") or not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            val = float(parts[0])
        except ValueError:
            continue
        unit, ev = parts[1], parts[2]
        if "cas_count" in ev:
            out[ev] = out.get(ev, 0.0) + to_bytes(val, unit)
        else:
            out[ev] = out.get(ev, 0.0) + val
    return out


def main():
    print(f"[*] Sweep 0..{MAX_AGG} aggressors; victim MemBench on core {VICTIM_CORE}.")
    victim = spawn_loop(VICTIM_CORE, MCF)
    time.sleep(3)
    aggs, rows = [], []
    try:
        for N in range(0, MAX_AGG + 1):
            for p in aggs:
                kill_tree(p)
            aggs = []
            time.sleep(1)
            for c in range(1, N + 1):
                aggs.append(spawn_loop(c, STREAM))
            time.sleep(3)                                   # stabilize
            perf_measure(("-C", str(VICTIM_CORE)), CORE_EVENTS, DURATION, "_v.txt")
            perf_measure(("-a",),                  IMC_EVENTS,  DURATION, "_imc.txt")
            v, m = parse("_v.txt"), parse("_imc.txt")
            cyc  = v.get("cycles", 0) or 1
            ipc  = v.get("instructions", 0) / cyc
            casb = sum(val for ev, val in m.items() if "cas_count" in ev)
            gbps = casb / 1e9 / DURATION
            llcm = v.get("LLC-load-misses", 0)
            print(f"    N={N}:  IPC={ipc:.3f}   BW={gbps:5.1f} GB/s   LLC-miss={llcm:,.0f}")
            rows.append([N, round(ipc, 4), round(gbps, 2),
                         int(llcm), int(v.get("LLC-loads", 0))])
    finally:
        for p in aggs:
            kill_tree(p)
        kill_tree(victim)
        subprocess.run(["pkill", "-f", MCF],    stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", STREAM], stderr=subprocess.DEVNULL)
        for f in ("_v.txt", "_imc.txt"):
            if os.path.exists(f):
                os.remove(f)
    with open("sweep.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n_aggressors", "victim_ipc", "mem_bw_GBps",
                    "llc_load_misses", "llc_loads"])
        w.writerows(rows)
    print("[+] WROTE sweep.csv  — send this file back.")


if __name__ == "__main__":
    main()
