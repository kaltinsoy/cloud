#!/usr/bin/env python3
"""
Exp 3 — IPC time-series across one full attack -> mitigate -> release cycle.
============================================================================
Produces the paper's first RESULTS figure (victim IPC collapse + recovery over
time). The reviewer noted there is currently no figure that animates Table II.

Timeline (victim MemBench on core 0; 5x STREAM aggressors on cores 1-5):
    t=0 ............ victim alone (baseline IPC)
    t=T_ATTACK ..... 5x STREAM launched          -> IPC collapses
    t=T_MITIGATE ... strangulate applied         -> IPC recovers
    t=T_RELEASE .... mitigation reset, aggressors stopped
    t=TOTAL ........ end

perf logs victim IPC every 200 ms (same format as telemetry.py, -I -x ,).

Run as root from the dir with mitigation_shield.py:
    sudo python3 exp3_timeseries.py
Send back:  ts_raw.csv     (phase offsets are fixed and printed at the end)
"""
import subprocess, os, time, signal

# ----------------------- CONFIG (adjust binary paths) -----------------------
VICTIM_CORE = 0
AGG_CORES   = [1, 2, 3, 4, 5]
MCF         = "/usr/local/bin/mcf"
STREAM      = "/home/lenovo/cloud2/STREAM/stream_c.exe"     # <-- ADJUST IF DIFFERENT
TOTAL       = 45                          # total seconds
T_ATTACK    = 8
T_MITIGATE  = 20
T_RELEASE   = 35
INTERVAL_MS = 200
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


def main():
    victim = spawn_loop(VICTIM_CORE, MCF)
    time.sleep(3)
    perf = subprocess.Popen(
        ["perf", "stat", "-I", str(INTERVAL_MS), "-x", ",",
         "-e", "instructions,cycles,LLC-load-misses", "-C", str(VICTIM_CORE),
         "-o", "ts_raw.csv", "--", "sleep", str(TOTAL)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    t0 = time.time()

    def at(sec):
        while time.time() - t0 < sec:
            time.sleep(0.02)

    aggs = []
    try:
        at(T_ATTACK)
        print(f"[{T_ATTACK}s] attack: launching {len(AGG_CORES)}x STREAM")
        aggs = [spawn_loop(c, STREAM) for c in AGG_CORES]

        at(T_MITIGATE)
        print(f"[{T_MITIGATE}s] mitigation: strangulate")
        subprocess.run(["python3", "-c",
            f"import mitigation_shield as m; "
            f"m.apply_policy_cores({AGG_CORES},'strangulate',victim_core={VICTIM_CORE})"])

        at(T_RELEASE)
        print(f"[{T_RELEASE}s] release: reset + stop aggressors")
        subprocess.run(["python3", "-c", "import mitigation_shield as m; m.reset_all()"])
        for p in aggs:
            kill_tree(p)
        aggs = []

        perf.wait()
    finally:
        for p in aggs:
            kill_tree(p)
        kill_tree(victim)
        subprocess.run(["pkill", "-f", MCF],    stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", STREAM], stderr=subprocess.DEVNULL)
        subprocess.run(["python3", "-c", "import mitigation_shield as m; m.reset_all()"],
                       stderr=subprocess.DEVNULL)

    print("[+] WROTE ts_raw.csv")
    print(f"PHASES (seconds):  attack={T_ATTACK}  mitigate={T_MITIGATE}  release={T_RELEASE}")
    print("    -> send ts_raw.csv back (these offsets are fixed, no need to send them)")


if __name__ == "__main__":
    main()
