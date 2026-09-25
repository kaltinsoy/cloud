#!/usr/bin/env python3
"""
Exp 9 — MBA calibration sweep on the Xeon Gold 6136 (reviewer Q1).
==================================================================
Empirical MBA studies (e.g., arXiv:2206.14637) show that on some Xeons only a
subset of MBA levels are actually effective. This measures, for a SOLO STREAM
on core 1 (no victim), the achieved IMC bandwidth at every MBA level
100..10 in steps of 10. Because STREAM runs alone, the measured system
bandwidth IS STREAM's own throughput, so this doubles as the aggressor-cost
measurement (supersedes exp8): how much the throttled tenant itself loses.

We report whatever comes out; nothing is assumed.

Run as root (pqos available), takes ~2 minutes:
    sudo python3 exp9_mba_calibration.py | tee exp9_mba_calibration.txt
Run it TWICE (repeatability):
    sudo python3 exp9_mba_calibration.py | tee exp9_mba_calibration_2.txt
Send back: exp9_mba_calibration*.txt
"""
import subprocess, os, time, signal

# ----------------------- CONFIG -----------------------
STREAM   = "/home/lenovo/cloud2/STREAM/stream_c.exe"   # <-- ADJUST to your STREAM binary
AGG_CORE = 1
DUR      = 6                          # seconds measured per level
LEVELS   = [None, 90, 80, 70, 60, 50, 40, 30, 20, 10, None]  # None = unthrottled (start & end check)
IMC = ",".join(f"uncore_imc_{i}/cas_count_read/,uncore_imc_{i}/cas_count_write/" for i in range(6))
# -------------------------------------------------------


def pq(*a):
    subprocess.run(["sudo", "pqos", "-I", *a], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_mba(mba):
    pq("-e", "llc:1=0x7FF")            # full cache -> isolate the pure MBA effect
    pq("-e", f"mba:1={mba}")
    pq("-a", f"llc:1={AGG_CORE}")


def reset():
    pq("-R")


def spawn(core, cmd):
    return subprocess.Popen(
        ["taskset", "-c", str(core), "bash", "-c", f"while true; do '{cmd}' >/dev/null 2>&1; done"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        start_new_session=True)


def kill_tree(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        pass


def to_bytes(v, u):
    u = u.strip()
    if u in ("GiB", "GB"): return v * 1073741824
    if u in ("MiB", "MB"): return v * 1048576
    if u in ("KiB", "KB"): return v * 1024
    return v


def measure_bw(dur):
    p = subprocess.run(["perf", "stat", "-x", ",", "-a", "-e", IMC, "--", "sleep", str(dur)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    total = 0.0
    for line in p.stderr.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            val = float(parts[0].replace("<not counted>", "0").replace("<not supported>", "0"))
        except ValueError:
            continue
        unit = parts[1]
        event = parts[2] if len(parts) > 2 else ""
        if "cas_count" in event:
            total += to_bytes(val, unit) if unit else val * 64
    return total / dur / 1e9


def main():
    print("[*] exp9 — MBA calibration sweep (solo STREAM on core %d)" % AGG_CORE)
    reset()
    agg = spawn(AGG_CORE, STREAM)
    time.sleep(3)

    base = None
    rows = []
    for lvl in LEVELS:
        if lvl is None:
            reset()
        else:
            apply_mba(lvl)
        time.sleep(1.5)
        bw = measure_bw(DUR)
        label = "unthrottled" if lvl is None else f"MBA {lvl}%"
        if base is None:
            base = bw
        pct = bw / base * 100 if base else 0.0
        print(f"  {label:12s}  BW = {bw:6.2f} GB/s   ({pct:5.1f}% of unthrottled)")
        rows.append((label, bw, pct))

    reset()
    kill_tree(agg)
    subprocess.run(["pkill", "-f", os.path.basename(STREAM)], stderr=subprocess.DEVNULL)

    print("\n===== MBA CALIBRATION TABLE =====")
    print(f"{'level':12s} {'GB/s':>7s} {'% of free':>10s}")
    for label, bw, pct in rows:
        print(f"{label:12s} {bw:7.2f} {pct:9.1f}%")
    print("\n(Adjacent levels with ~equal BW = ineffective steps on this CPU.)")
    print("Send: exp9_mba_calibration*.txt")


if __name__ == "__main__":
    main()
