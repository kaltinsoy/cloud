#!/usr/bin/env python3
"""
collect_results.py — Bir senaryonun mitigation sonuçlarını toplar.
================================================================
run_scenarios.sh tarafından çağrılır:
    python3 collect_results.py <label> <utc_start>

Dashboard'un /api/events API'sinden mitigation_result event'lerini çeker,
verilen UTC zaman penceresindekileri (ts >= utc_start) süzer, recovery
yüzdelerinin mean ± std'sini hesaplar.

Çıktı:
    - ekrana özet
    - paper_results.txt   (append, insan-okur)
    - paper_results_<label>.json (ham)

Sadece Python stdlib kullanır (urllib, json, statistics) — ek paket yok.
"""
import sys
import json
import statistics
import urllib.request
from collections import Counter

DASHBOARD = "http://localhost:5000"
RESULTS_TXT = "paper_results.txt"


def fetch_events():
    with urllib.request.urlopen(f"{DASHBOARD}/api/events", timeout=8) as r:
        return json.load(r)


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "scenario"
    start = sys.argv[2] if len(sys.argv) > 2 else ""
    out_txt = sys.argv[3] if len(sys.argv) > 3 else RESULTS_TXT

    try:
        events = fetch_events()
    except Exception as e:
        msg = f"[!] {label}: API'den event çekilemedi: {e}"
        print(msg)
        with open(out_txt, "a") as f:
            f.write(msg + "\n")
        return

    results = []
    for ev in events:
        if ev.get("type") != "mitigation_result":
            continue
        if start and ev.get("ts", "") < start:
            continue
        p = ev.get("payload", {}) or {}
        rec = p.get("recovery_percent")
        if rec is None:
            continue
        results.append({
            "recovery": float(rec),
            "status": p.get("status", "?"),
            "before": float(p.get("before_ipc", 0) or 0),
            "after": float(p.get("after_ipc", 0) or 0),
            "ts": ev.get("ts", ""),
        })

    lines = []
    lines.append("=" * 60)
    lines.append(f"SENARYO: {label}")
    lines.append(f"  mitigation_result event sayisi: {len(results)}")

    if results:
        recs = [r["recovery"] for r in results]
        mean = statistics.mean(recs)
        std = statistics.stdev(recs) if len(recs) >= 2 else 0.0
        sc = Counter(r["status"] for r in results)
        lines.append(f"  Recovery (mean +/- std): {mean:+.1f}% +/- {std:.1f}%")
        lines.append(f"  Recovery (min / max):    {min(recs):+.1f}% / {max(recs):+.1f}%")
        lines.append("  Status: " + ", ".join(f"{k}={v}" for k, v in sc.items()))
        lines.append("  Tekil event'ler:")
        for r in results:
            lines.append(
                f"    IPC {r['before']:.3f} -> {r['after']:.3f}   "
                f"recovery {r['recovery']:+.1f}%   [{r['status']}]"
            )
    else:
        lines.append("  (Bu pencerede mitigation_result yok — saldiri "
                     "tetiklenmemis olabilir; DURATION'i artir ya da "
                     "workload'lari kontrol et.)")

    block = "\n".join(lines)
    print(block)

    with open(out_txt, "a") as f:
        f.write(block + "\n")

    with open(f"paper_results_{label}.json", "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
