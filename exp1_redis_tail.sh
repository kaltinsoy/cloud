#!/usr/bin/env bash
# Exp 1 — Redis tail latency (p99) under bandwidth contention, +/- mitigation.
# ============================================================================
# The paper currently reports Redis only in IPC, which is a weak proxy for a
# latency-bound service (the Verifier flags it "ineffective"). This measures the
# PROPER SLO metric: p99 latency and throughput, in three conditions, so the
# Redis rows can say something meaningful instead of "nothing happened".
#
# Run from the directory that contains mitigation_shield.py. Needs sudo for the
# mitigation step.
#     bash exp1_redis_tail.sh
# Send back:  redis_A_solo.txt  redis_B_attacked.txt  redis_C_mitigated.txt
set -u

# ----------------------- CONFIG (adjust STREAM path) -----------------------
VICTIM_CORE=0
AGG_CORES="1 2 3 4 5"
STREAM=/home/lenovo/cloud2/STREAM/stream_c.exe     # <-- ADJUST IF DIFFERENT
PORT=6379
REQS=1000000
CLIENTS=50
# ---------------------------------------------------------------------------

start_stream(){ for c in $AGG_CORES; do taskset -c $c bash -c "while true; do $STREAM >/dev/null 2>&1; done" & done; }
stop_stream(){  pkill -f "$STREAM" 2>/dev/null; sleep 1; }
apply_mit(){ sudo python3 -c "import mitigation_shield as m; m.apply_policy_cores([1,2,3,4,5],'strangulate',victim_core=$VICTIM_CORE)"; }
reset_mit(){ sudo python3 -c "import mitigation_shield as m; m.reset_all()" 2>/dev/null; }
bench(){ echo "[*] bench: $1"; redis-benchmark -p $PORT -t get,set -n $REQS -c $CLIENTS -P 1 > "redis_$1.txt" 2>&1; echo "    -> redis_$1.txt"; }

# launch redis pinned to the victim core
taskset -c $VICTIM_CORE redis-server --save '' --appendonly no --port $PORT >/dev/null 2>&1 &
REDIS_PID=$!
sleep 2
redis-benchmark -p $PORT -t set -n 100000 -q >/dev/null   # warm-up

# (A) SOLO — no aggressor, no mitigation
reset_mit; stop_stream
bench A_solo

# (B) ATTACKED — 5x STREAM, no mitigation
start_stream; sleep 3
bench B_attacked

# (C) MITIGATED — 5x STREAM + strangulate
apply_mit; sleep 3
bench C_mitigated

# cleanup
reset_mit; stop_stream
redis-cli -p $PORT shutdown nosave 2>/dev/null; kill $REDIS_PID 2>/dev/null

echo ""
echo "DONE. Send: redis_A_solo.txt  redis_B_attacked.txt  redis_C_mitigated.txt"
echo "(each file has 'requests per second' + a 'Latency by percentile distribution' table incl. p99)"
