# -*- coding: utf-8 -*-
"""Fig. 1 (architecture), drawn at print size so labels stay >= 6.5 pt at 7.0 in wide.
Same content as fig1_architecture_v2.mmd; the Verifier's return edge is 'continue monitoring
(verdict logged)' because release is hysteresis-driven, not verdict-driven."""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fig1.png")
plt.rcParams.update({"font.family": ["Arial", "DejaVu Sans"]})
W, H = 7.0, 1.95
FS = 7.2      # node text (pt)
FL = 6.6      # edge labels (pt)

C = {  # fill, edge, text  (same palette as the mermaid classDefs)
    "loop": ("#eaf1fb", "#3b6fb5", "#1f2733"),
    "mit":  ("#fdecec", "#c0473f", "#1f2733"),
    "ver":  ("#e7f6ef", "#1f9d6b", "#0f6b48"),
    "vic":  ("#e7f6ef", "#1f9d6b", "#0f6b48"),
    "agg":  ("#fdecec", "#c0473f", "#9c302a"),
    "shr":  ("#eaeefb", "#5566c4", "#34408f"),
}

fig = plt.figure(figsize=(W, H))
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")

def node(cx, cy, w, h, text, cls, lw=0.9, bold_first=True):
    f, e, t = C[cls]
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h, boxstyle="round,pad=0,rounding_size=0.04",
                                fc=f, ec=e, lw=lw, zorder=2))
    first, _, rest = text.partition("\n")
    ax.text(cx, cy + (0.075 if rest else 0), first, ha="center", va="center", fontsize=FS, color=t,
            fontweight="bold" if bold_first else "normal", zorder=3)
    if rest:
        ax.text(cx, cy - 0.085, rest, ha="center", va="center", fontsize=FS - 0.4, color=t, zorder=3)
    return dict(l=cx - w / 2, r=cx + w / 2, t=cy + h / 2, b=cy - h / 2, cx=cx, cy=cy)

def arrow(p, q, dotted=False, color="#222222", lw=0.9, conn="arc3"):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=7, lw=lw, color=color,
                                 linestyle=(0, (1.2, 1.4)) if dotted else "-", connectionstyle=conn,
                                 shrinkA=0, shrinkB=0, zorder=4))

def label(x, y, text, ha="center"):
    ax.text(x, y, text, ha=ha, va="center", fontsize=FL, color="#333333", zorder=5,
            bbox=dict(boxstyle="round,pad=0.12", fc="#e6e6e6", ec="none"))

# ---------------- host box ----------------
hx0, hx1, hy0, hy1 = 0.04, W - 0.04, 1.10, 1.91
ax.add_patch(FancyBboxPatch((hx0, hy0), hx1 - hx0, hy1 - hy0, boxstyle="round,pad=0,rounding_size=0.03",
                            fc="#f7f7fa", ec="#2b2b3a", lw=0.9, zorder=1))
ax.text(W / 2, hy1 - 0.1, "Host: dual Xeon Gold 6136 — experiments on one NUMA node "
        "(12 cores · 24.75 MB L3 · 2 DDR4-2666 channels)", ha="center", va="center", fontsize=FL, color="#222222")
nh = 0.40; ny = 1.40
SH = node(1.05, ny, 1.75, nh, "Shared LLC + memory bus\nIMC · 2 populated channels", "shr")
AC = node(4.60, ny, 1.15, nh, "Aggressor cores\n1..N", "agg")
VC = node(6.35, ny, 0.95, nh, "Victim\ncore 0", "vic")

# ---------------- pipeline row ----------------
py, ph = 0.55, 0.44
specs = [("1 · Telemetry\nperf: PMU + IMC", "loop", 1.10),
         ("2 · Detection\nRandom Forest", "loop", 1.00),
         ("3 · Hysteresis + localize\nK=3 / M=10 · find aggressor", "loop", 1.52),
         ("4 · Mitigation\nCAT + MBA via resctrl", "mit", 1.30),
         ("5 · Verifier\nIPC recovery — effective?", "ver", 1.36)]
x0, x1 = 0.08, W - 0.08
gap = (x1 - x0 - sum(s[2] for s in specs)) / (len(specs) - 1)
boxes, x = [], x0
for text, cls, w in specs:
    boxes.append(node(x + w / 2, py, w, ph, text, cls, lw=1.6 if cls == "ver" else 0.9))
    x += w + gap
T, D, Hh, M, V = boxes
for a, b in zip(boxes, boxes[1:]):
    arrow((a["r"], py), (b["l"], py))

# ---------------- vertical edges ----------------
arrow((SH["cx"] - 0.25, SH["b"]), (T["cx"], T["t"]))
label((SH["cx"] - 0.25 + T["cx"]) / 2 + 0.02, (SH["b"] + T["t"]) / 2 + 0.01, "PMU + IMC counters")
arrow((M["cx"], M["t"]), (AC["cx"] - 0.1, AC["b"]))
label((M["cx"] + AC["cx"] - 0.1) / 2, (M["t"] + AC["b"]) / 2 + 0.01, "CAT + MBA")
arrow((V["cx"], V["t"]), (VC["cx"], VC["b"]), dotted=True)
label((V["cx"] + VC["cx"]) / 2, (V["t"] + VC["b"]) / 2 + 0.01, "measure victim IPC")

# ---------------- return edge: monitoring continues (release is hysteresis-driven) ----------------
yb = 0.13
ax.plot([V["cx"], V["cx"], T["cx"]], [V["b"], yb, yb], color="#222222", lw=0.9,
        linestyle=(0, (1.2, 1.4)), zorder=4)
arrow((T["cx"], yb), (T["cx"], T["b"]), dotted=True)
label((T["cx"] + V["cx"]) / 2, yb, "continue monitoring (verdict logged; release via hysteresis)")

fig.savefig(OUT, dpi=600)
print("saved", OUT)
