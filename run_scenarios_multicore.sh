#!/bin/bash
# ================================================================
# Noisy-Neighbor — ÇOK-CORE Senaryo Koşucusu
# ================================================================
# Tek-core STREAM bu Xeon'da (6 kanal, ~120 GB/s) kurbanı zorlamıyordu.
# Bu script ATTACKER'ı 5 core'da (1-5) çalıştırıp memory bus'ı GERÇEKTEN
# doyurur → kurban gerçekten aç kalır → mitigation throttle edince
# dramatik GERÇEK recovery görünür.
#
# ÖNKOŞUL (çok-core farkındalıklı daemon!):
#     python3 app.py
#     sudo python3 live_daemon2.py      # ATTACKER_CORES=[1,2,3,4,5] olan yeni hali
#
# KULLANIM:
#     cd ~/cloud2
#     sudo bash run_scenarios_multicore.sh
#
# ÇIKTI: paper_results_multicore.txt + paper_results_<senaryo>.json
# Süre ≈ 9 dk (2 senaryo × 5 tekrar).
# ================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="python3"
DASHBOARD="http://localhost:5000"

REPS=5
ATTACK_WAIT=35
RELEASE_WAIT=18
WARMUP=8
COOLDOWN=12
ATTACKER_CORES="1 2 3 4 5"     # 5x STREAM — daemon da bu core'ları mitigate ediyor

STREAM_BIN="$SCRIPT_DIR/STREAM/stream_c.exe"
[ -x "$STREAM_BIN" ] || STREAM_BIN="$HOME/cloud2/STREAM/stream_c.exe"

RESULTS_TXT="$SCRIPT_DIR/paper_results_multicore.txt"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'

if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}[-] sudo gerekli: sudo bash run_scenarios_multicore.sh${NC}"; exit 1
fi
if ! "$PYTHON_BIN" -c "import urllib.request;urllib.request.urlopen('$DASHBOARD/api/stats',timeout=5)" >/dev/null 2>&1; then
    echo -e "${RED}[-] Dashboard yanıt vermiyor: $DASHBOARD${NC}"
    echo "    Önce: python3 app.py  ve  sudo python3 live_daemon2.py"
    exit 1
fi
if [ ! -f "$SCRIPT_DIR/collect_results.py" ]; then
    echo -e "${RED}[-] collect_results.py yok.${NC}"; exit 1
fi
if [ ! -x "$STREAM_BIN" ]; then
    echo -e "${RED}[-] STREAM binary yok: $STREAM_BIN — bu script STREAM'siz çalışmaz.${NC}"; exit 1
fi

kill_attackers() { pkill -9 -f "stream_c" 2>/dev/null; }
kill_all() {
    pkill -9 -f "mcf inp"          2>/dev/null
    pkill -9 -f "stream_c"         2>/dev/null
    pkill -9 -f "redis-benchmark"  2>/dev/null
    sleep 1
}
trap 'echo; echo -e "${YELLOW}[*] İptal — temizleniyor...${NC}"; kill_all; exit 130' INT TERM

echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor >/dev/null 2>&1 || true

# 5 core'da STREAM başlat
start_attackers() {
    local c
    for c in $ATTACKER_CORES; do
        bash -c "while true; do numactl --physcpubind=$c '$STREAM_BIN' >/dev/null 2>&1; done" &
    done
}

run_scenario() {
    local label="$1"; local victim_cmd="$2"
    echo ""
    echo -e "${CYAN}========================================================${NC}"
    echo -e "${CYAN}  SENARYO: $label   (${REPS} tekrar, attacker cores: $ATTACKER_CORES)${NC}"
    echo -e "${CYAN}========================================================${NC}"
    kill_all
    local start; start="$(date -u +%Y-%m-%dT%H:%M:%S)"

    echo -e "${GREEN}[*] Victim başlatılıyor (core 0), ${WARMUP}s ısınma...${NC}"
    bash -c "$victim_cmd" &
    sleep "$WARMUP"

    local rep
    for rep in $(seq 1 "$REPS"); do
        echo -e "${GREEN}[*] [$label] rep $rep/$REPS — 5x STREAM AÇ (cores $ATTACKER_CORES)${NC}"
        start_attackers
        sleep "$ATTACK_WAIT"
        echo -e "${YELLOW}[*] [$label] rep $rep/$REPS — STREAM KAPAT (release bekleniyor)${NC}"
        kill_attackers
        sleep "$RELEASE_WAIT"
    done

    echo -e "${GREEN}[*] Victim durduruluyor...${NC}"
    kill_all
    sleep 3

    echo -e "${GREEN}[*] Sonuçlar toplanıyor...${NC}"
    "$PYTHON_BIN" "$SCRIPT_DIR/collect_results.py" "$label" "$start" "$RESULTS_TXT"
    sleep "$COOLDOWN"
}

MCF_VICTIM='while true; do numactl --physcpubind=0 mcf inp.in >/dev/null 2>&1; done'
REDIS_VICTIM='while true; do numactl --physcpubind=0 redis-benchmark -t set,get -n 100000000 -q >/dev/null 2>&1; done'

{
  echo "Noisy-Neighbor — MULTI-CORE Paper Results (REPS=$REPS, attackers=$ATTACKER_CORES)"
  echo "Tarih: $(date)"
  echo "Ayar: samples_per_phase=7, policy=strangulate, ATTACK_WAIT=${ATTACK_WAIT}s"
} > "$RESULTS_TXT"

run_scenario "mcf_vs_5xSTREAM"    "$MCF_VICTIM"
run_scenario "redis_vs_5xSTREAM"  "$REDIS_VICTIM"

kill_all
echo ""
echo -e "${GREEN}========================================================${NC}"
echo -e "${GREEN}  ÇOK-CORE SENARYOLAR BİTTİ${NC}"
echo -e "${GREEN}========================================================${NC}"
echo ""
cat "$RESULTS_TXT"
echo ""
echo -e "${CYAN}>>> paper_results_multicore.txt + paper_results_*.json dosyalarını Kazım'a at.${NC}"
