# -*- coding: utf-8 -*-
"""Fig. 2 from raw data: (a) aggressor-count sweep (sweep.csv), (b) cache x bandwidth factorial
(exp6_results_{1,2,3}.txt). Bars = per-phase medians over the three runs (as Table II);
error bars = min-max across runs (attacked: across all 15 attacked windows)."""
import re, statistics, os
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")   # repo root: sweep.csv, exp6_results_*.txt
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fig_results.png")

plt.rcParams.update({"font.family": ["Arial", "DejaVu Sans"], "font.size": 7.5, "axes.titlesize": 8,
                     "axes.labelsize": 7.5, "xtick.labelsize": 7, "ytick.labelsize": 7,
                     "axes.linewidth": 0.6, "xtick.major.width": 0.6, "ytick.major.width": 0.6})
RED, BLUE, GREEN = "#c0473f", "#3a6fb5", "#1f9d6b"

# ---------------- (a) sweep ----------------
sw = pd.read_csv(os.path.join(DL, "sweep.csv"))

# ---------------- (b) factorial ----------------
phases = {"attacked": [], "strangulate": [], "mba20_nocat": [], "strangulate_hard": [], "mba10_nocat": []}
for k in (1, 2, 3):
    txt = open(os.path.join(DL, f"exp6_results_{k}.txt"), encoding="utf-8").read()
    table = txt.split("2x2 VERDICT")[1].split("--- CACHE EFFECT")[0]
    for m in re.finditer(r"^(\w+)\s+([\d.]+)%", table, re.M):
        name, val = m.group(1), float(m.group(2))
        if name in phases:
            phases[name].append(val)
assert len(phases["attacked"]) == 15 and all(len(v) == 3 for n, v in phases.items() if n != "attacked")
order = ["attacked", "strangulate", "mba20_nocat", "strangulate_hard", "mba10_nocat"]
labels = ["attacked", "strangulate\n0x003+20%", "MBA-only\n0x7FF+20%", "strang.-hard\n0x003+10%", "MBA-hard\n0x7FF+10%"]
med = [statistics.median(phases[n]) for n in order]
lo = [m - min(phases[n]) for m, n in zip(med, order)]
hi = [max(phases[n]) - m for m, n in zip(med, order)]
colors = [RED, "#9aa7d8", "#6f86c8", GREEN, BLUE]

fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.0, 2.35), gridspec_kw={"width_ratios": [0.9, 1.3], "wspace": 0.5})

a1.plot(sw.n_aggressors, sw.victim_ipc, "-o", color=RED, ms=3.2, lw=1.1)
a1.set_xlabel("number of STREAM aggressors")
a1.set_ylabel("victim IPC", color=RED)
a1.tick_params(axis="y", colors=RED)
a1.set_ylim(0, 0.30)
a1.set_xticks(range(0, 7))
a1b = a1.twinx()
a1b.plot(sw.n_aggressors, sw.mem_bw_GBps, "-s", color=BLUE, ms=3.0, lw=1.1)
a1b.set_ylabel("system memory BW (GB/s)", color=BLUE)
a1b.tick_params(axis="y", colors=BLUE)
a1b.set_ylim(0, 40)
a1.set_title("(a) Intensity sweep: knee at N≈3")
for sp in ("top",):
    a1.spines[sp].set_visible(False); a1b.spines[sp].set_visible(False)

x = range(len(order))
a2.bar(x, med, color=colors, edgecolor="#333333", linewidth=0.5, width=0.72,
       yerr=[lo, hi], capsize=2, error_kw={"elinewidth": 0.7, "capthick": 0.7})
for xi, m in zip(x, med):
    a2.text(xi, m + 2.5, f"{m:.1f}%", ha="center", va="bottom", fontsize=7, fontweight="bold")
a2.axhline(100, ls="--", lw=0.8, color=GREEN)
a2.text(4.45, 101.5, "solo baseline", ha="right", va="bottom", fontsize=6.5, color=GREEN)
a2.set_xticks(list(x)); a2.set_xticklabels(labels, fontsize=6.2)
a2.set_ylim(0, 112)
a2.set_ylabel("victim IPC (% of solo baseline)")
a2.set_title("(b) Policies under 5×STREAM (median of 3 runs)")
a2.spines["top"].set_visible(False); a2.spines["right"].set_visible(False)

fig.savefig(OUT, dpi=600, bbox_inches="tight", pad_inches=0.02)
print("saved", OUT)
print("medians", [round(m, 1) for m in med], "min", [min(phases[n]) for n in order], "max", [max(phases[n]) for n in order])
