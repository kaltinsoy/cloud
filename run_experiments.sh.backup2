#!/bin/bash
# ============================================================
# Noisy-Neighbor Experiment Matrix Runner (v2 — path fix)
# ============================================================
#
# v2 FIX: Script kendi dizinini otomatik tespit eder
#         (eskisi ~/cloud hardcoded kullanıyordu, ~/cloud2'de çalışmıyordu)
#
# Matrix: 4 victim × 4 attacker × 10 run = 160 experiment, ~173 dakika
# ============================================================

set -u

# --- Script'in kendi dizinini bul (HARDCODED YOK!) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLOUD_DIR="$SCRIPT_DIR"
TELEMETRY_PY="${CLOUD_DIR}/telemetry.py"

# Python — venv varsa kullan, yoksa system
if [ -x "${CLOUD_DIR}/telemetry_env/bin/python" ]; then
    PYTHON_BIN="${CLOUD_DIR}/telemetry_env/bin/python"
elif [ -x "${HOME}/cloud/telemetry_env/bin/python" ]; then
    PYTHON_BIN="${HOME}/cloud/telemetry_env/bin/python"
elif [ -x "${HOME}/cloud2/telemetry_env/bin/python" ]; then
    PYTHON_BIN="${HOME}/cloud2/telemetry_env/bin/python"
else
    PYTHON_BIN="python3"
fi

# STREAM binary — birkaç yere bak
if [ -x "${CLOUD_DIR}/STREAM/stream_c.exe" ]; then
    STREAM_BIN="${CLOUD_DIR}/STREAM/stream_c.exe"
elif [ -x "${HOME}/cloud/STREAM/stream_c.exe" ]; then
    STREAM_BIN="${HOME}/cloud/STREAM/stream_c.exe"
else
    STREAM_BIN="${CLOUD_DIR}/STREAM/stream_c.exe"  # default, kontrol et
fi

VICTIMS=("sysbench" "redis-benchmark" "mcf" "x264")
ATTACKERS=("none" "STREAM" "stress-ng" "mcf-attacker")
RUNS=10

VICTIM_CORE=0
ATTACKER_CORE=1
NUMA_NODE=0
DURATION=60
COOLDOWN=5

TOTAL_EXPERIMENTS=$((${#VICTIMS[@]} * ${#ATTACKERS[@]} * RUNS))
COMPLETED=0
SKIPPED=0
FAILED=0

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo "========================================================"
echo "  Noisy-Neighbor Experiment Matrix Runner"
echo "========================================================"
echo "  Script dir:    $SCRIPT_DIR"
echo "  Python:        $PYTHON_BIN"
echo "  Telemetry:     $TELEMETRY_PY"
echo "  STREAM binary: $STREAM_BIN"
echo "--------------------------------------------------------"
echo "  Victims:   ${VICTIMS[@]}"
echo "  Attackers: ${ATTACKERS[@]}"
echo "  Runs per pair: $RUNS"
echo "  Toplam experiment: $TOTAL_EXPERIMENTS"
echo "  Tahmini süre: $((TOTAL_EXPERIMENTS * (DURATION + COOLDOWN) / 60)) dakika"
echo "========================================================"

# Sudo check
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}[-] sudo gerekli. 'sudo bash run_experiments.sh' çalıştırın.${NC}"
    exit 1
fi

# Telemetry.py var mı kontrol
if [ ! -f "$TELEMETRY_PY" ]; then
    echo -e "${RED}[-] telemetry.py bulunamadı: $TELEMETRY_PY${NC}"
    exit 1
fi

echo -e "${CYAN}[*] Telemetry script doğrulaması:${NC}"
head -3 "$TELEMETRY_PY"
echo "..."

# --- Workload availability ---
check_workload() {
    local name="$1"
    case "$name" in
        sysbench)        command -v sysbench >/dev/null 2>&1 ;;
        redis-benchmark) command -v redis-benchmark >/dev/null 2>&1 ;;
        mcf|mcf-attacker) command -v mcf >/dev/null 2>&1 ;;
        x264)            command -v x264 >/dev/null 2>&1 ;;
        STREAM)          [ -x "$STREAM_BIN" ] ;;
        stress-ng)       command -v stress-ng >/dev/null 2>&1 ;;
        none)            true ;;
        *)               false ;;
    esac
}

# --- Victim launcher ---
launch_victim() {
    local victim="$1"
    case "$victim" in
        sysbench)
            while true; do
                numactl --physcpubind=$VICTIM_CORE --membind=$NUMA_NODE \
                    sysbench cpu --cpu-max-prime=50000 run > /dev/null 2>&1
            done &
            ;;
        redis-benchmark)
            while true; do
                numactl --physcpubind=$VICTIM_CORE --membind=$NUMA_NODE \
                    redis-benchmark -t set,get -n 100000000 -q > /dev/null 2>&1
            done &
            ;;
        mcf)
            while true; do
                numactl --physcpubind=$VICTIM_CORE --membind=$NUMA_NODE \
                    mcf inp.in > /dev/null 2>&1
            done &
            ;;
        x264)
            while true; do
                numactl --physcpubind=$VICTIM_CORE --membind=$NUMA_NODE \
                    x264 --crf 20 -o /dev/null /tmp/test_video.y4m > /dev/null 2>&1
            done &
            ;;
        *)
            echo -e "${RED}[-] Bilinmeyen victim: $victim${NC}"
            return 1
            ;;
    esac
    echo $!
}

# --- Attacker launcher ---
launch_attacker() {
    local attacker="$1"
    case "$attacker" in
        none)
            echo ""
            ;;
        STREAM)
            while true; do
                numactl --physcpubind=$ATTACKER_CORE --membind=$NUMA_NODE \
                    "$STREAM_BIN" > /dev/null 2>&1
            done &
            echo $!
            ;;
        stress-ng)
            numactl --physcpubind=$ATTACKER_CORE --membind=$NUMA_NODE \
                stress-ng --matrix 1 --cache 1 > /dev/null 2>&1 &
            echo $!
            ;;
        mcf-attacker)
            while true; do
                numactl --physcpubind=$ATTACKER_CORE --membind=$NUMA_NODE \
                    mcf inp.in > /dev/null 2>&1
            done &
            echo $!
            ;;
        *)
            echo -e "${RED}[-] Bilinmeyen attacker: $attacker${NC}" >&2
            return 1
            ;;
    esac
}

# --- Cleanup ---
cleanup_workloads() {
    local victim_pid="$3"
    local attacker_pid="$4"

    [ -n "$victim_pid" ] && kill -9 "$victim_pid" 2>/dev/null
    [ -n "$attacker_pid" ] && kill -9 "$attacker_pid" 2>/dev/null

    pkill -9 -f "sysbench cpu" 2>/dev/null
    pkill -9 -f "redis-benchmark" 2>/dev/null
    pkill -9 -f "stream_c" 2>/dev/null
    pkill -9 -f "stress-ng" 2>/dev/null
    pkill -9 -f "mcf inp" 2>/dev/null
    pkill -9 -f "x264 --crf" 2>/dev/null

    sleep 1
}

# --- Ana döngü ---
START_TIME=$(date +%s)

for victim in "${VICTIMS[@]}"; do
    if ! check_workload "$victim"; then
        echo -e "${YELLOW}[!] $victim yüklü değil — $(( ${#ATTACKERS[@]} * RUNS )) deney atlanıyor.${NC}"
        SKIPPED=$(( SKIPPED + ${#ATTACKERS[@]} * RUNS ))
        continue
    fi

    for attacker in "${ATTACKERS[@]}"; do
        if ! check_workload "$attacker"; then
            echo -e "${YELLOW}[!] $attacker yüklü değil — $RUNS deney atlanıyor.${NC}"
            SKIPPED=$(( SKIPPED + RUNS ))
            continue
        fi

        for run in $(seq 1 $RUNS); do
            echo ""
            echo "--------------------------------------------------------"
            echo "[$(date '+%H:%M:%S')] [$((COMPLETED + 1))/$TOTAL_EXPERIMENTS] "\
                 "$victim (core $VICTIM_CORE) vs $attacker (core $ATTACKER_CORE) — run $run"
            echo "--------------------------------------------------------"

            ATTACKER_PID=""
            if [ "$attacker" != "none" ]; then
                ATTACKER_PID=$(launch_attacker "$attacker")
            fi

            VICTIM_PID=$(launch_victim "$victim")
            if [ -z "$VICTIM_PID" ]; then
                echo -e "${RED}[-] Victim başlatılamadı.${NC}"
                FAILED=$((FAILED + 1))
                continue
            fi

            sleep 2

            # ÖNEMLİ: kendi PYTHON_BIN ve kendi TELEMETRY_PY'yi kullan
            if "$PYTHON_BIN" "$TELEMETRY_PY" \
                --victim "$victim" \
                --attacker "$attacker" \
                --run_id "$run" \
                --duration "$DURATION"; then
                COMPLETED=$((COMPLETED + 1))
                echo -e "${GREEN}[✓] Deney tamamlandı.${NC}"
            else
                FAILED=$((FAILED + 1))
                echo -e "${RED}[-] Telemetry başarısız.${NC}"
            fi

            cleanup_workloads "$victim" "$attacker" "$VICTIM_PID" "$ATTACKER_PID"

            echo "[*] $COOLDOWN saniye cooldown..."
            sleep $COOLDOWN
        done
    done
done

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

echo ""
echo "========================================================"
echo "  Experiment Matrix Tamamlandı"
echo "========================================================"
echo "  Tamamlanan:  $COMPLETED"
echo "  Atlanan:     $SKIPPED"
echo "  Başarısız:   $FAILED"
echo "  Toplam:      $TOTAL_EXPERIMENTS"
echo "  Geçen süre:  $((ELAPSED / 60)) dakika $((ELAPSED % 60)) saniye"
echo "========================================================"
echo ""
echo "Sıradaki adım:"
echo "  $PYTHON_BIN $CLOUD_DIR/train_model.py"
echo ""
