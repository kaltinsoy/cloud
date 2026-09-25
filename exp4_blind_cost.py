#!/usr/bin/env python3
"""
Exp 4 (optional) — the COST of blind (unnecessary) mitigation.
==============================================================
The paper's whole motivation is that mitigating when it isn't needed is harmful,
but that harm is never measured. This shows it directly: with a SINGLE aggressor
(1x STREAM on core 1) the victim is barely harmed, yet if we strangulate anyway
we throttle the aggressor's throughput for ~zero victim benefit.

Measures victim IPC and system memory bandwidth BEFORE vs AFTER an (unnecessary)
strangulate. Expected: victim IPC ~unchanged, system bandwidth drops -> the
throughput a verdict-blind controller would waste.

Run as root from the dir with mitigation_shield.py:
    sudo python3 exp4_blind_cost.py | tee blind_cost.txt
Send back:  the printed BEFORE/AFTER table (or blind_cost.txt)
"""
import subprocess, os, time, signal

# ----------------------- CONFIG (adjust binary paths) -----------------------
VICTIM_CORE = 0
AGG_CORE    = 1
MCF         = "/usr/local/bin/mcf"
STREAM      = "/home/lenovo/cloud2/STREAM/stream_c.exe"     # <-- ADJUST IF DIFFERENT
DURATION    = 10
CORE_EVENTS = "instructions,cycles,LLC-load-misses,LLC-loads"
IMC_EVENTS  = ",".join(
    f"uncore_imc_{i}/cas_count_read/,uncore_imc_{i}/cas_count_write/" for i in range(6)
)
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


def perf_measure(scope, events, dur, outfile):
    cmd = ["perf", "stat", "-x", ",", "-e", events, *scope, "--", "sleep", str(dur)]
    with open(outfile, "w") as err:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=err)


def to_bytes(val, unit):
    u = unit.strip()
    if u in ("GiB", "GB"):  return val * 1073741824
    if u in ("MiB", "MB"):  return val * 1048576
    if u in ("KiB", "KB"):  return val * 1024
    if u in ("B", "Bytes"): return val
    return val * 64


def parse(path):
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


def measure():
    perf_measure(("-C", str(VICTIM_CORE)), CORE_EVENTS, DURATION, "_v.txt")
    perf_measure(("-a",),                  IMC_EVENTS,  DURATION, "_imc.txt")
    v, m = parse("_v.txt"), parse("_imc.txt")
    ipc  = v.get("instructions", 0) / (v.get("cycles", 0) or 1)
    casb = sum(val for ev, val in m.items() if "cas_count" in ev)
    gbps = casb / 1e9 / DURATION
    for f in ("_v.txt", "_imc.txt"):
        if os.path.exists(f):
            os.remove(f)
    return ipc, gbps


def main():
    victim = spawn_loop(VICTIM_CORE, MCF)
    agg    = spawn_loop(AGG_CORE, STREAM)
    time.sleep(3)
    try:
        ipc0, bw0 = measure()
        print(f"BEFORE (no mitigation):  victim IPC = {ipc0:.3f}   system BW = {bw0:5.1f} GB/s")

        subprocess.run(["python3", "-c",
            f"import mitigation_shield as m; "
            f"m.apply_policy_cores([{AGG_CORE}],'strangulate',victim_core={VICTIM_CORE})"])
        time.sleep(3)

        ipc1, bw1 = measure()
        print(f"AFTER  (strangulate):    victim IPC = {ipc1:.3f}   system BW = {bw1:5.1f} GB/s")

        dv = (ipc1 - ipc0) / ipc0 * 100 if ipc0 else 0.0
        db = (bw1 - bw0) / bw0 * 100 if bw0 else 0.0
        print()
        print(f"Victim IPC change:           {dv:+.1f}%   (~0  => mitigation gave the victim no benefit)")
        print(f"Aggressor/system BW change:  {db:+.1f}%   (negative => throughput wasted by blind mitigation)")
    finally:
        subprocess.run(["python3", "-c", "import mitigation_shield as m; m.reset_all()"],
                       stderr=subprocess.DEVNULL)
        kill_tree(victim)
        kill_tree(agg)
        subprocess.run(["pkill", "-f", MCF],    stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", STREAM], stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
