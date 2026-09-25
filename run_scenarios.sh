#!/bin/bash
# ================================================================
# Noisy-Neighbor — Otomatik Senaryo Koşucusu (v2 — REPS)
# ================================================================
# Her senaryoda VICTIM sürekli çalışır; ATTACKER 5 kez aç-kapat
# yapılır. Her aç-kapat = 1 bağımsız trigger->mitigate->result.
# Böylece her senaryodan N=5 temiz ölçüm → düzgün mean ± std.
#
# (v1'de oscillation'a güveniyorduk; STREAM throttle edilince bile
#  "saldırı" görünüp release etmediği için tek event veriyordu.)
#
# ÖNKOŞUL (başka 2 terminalde ZATEN çalışıyor olmalı):
#     python3 app.py
#     sudo python3 live_daemon2.py
#
# KULLANIM:
#     cd ~/cloud2
#     sudo bash run_scenarios.sh
#
# ÇIKTI: paper_results.txt + paper_results_<senaryo>.json
# Toplam süre ≈ 14 dk.
# ================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="python3"
DASHBOARD="http://localhost:5000"

# --- Ayarlar ---
REPS=5            # her senaryoda kaç bağımsız attacker aç-kapat
ATTACK_WAIT=35    # attacker açıkken bekleme (detect + verify için; verify ~17s)
RELEASE_WAIT=18   # attacker kapandıktan sonra (mitigation release için, M=10)
WARMUP=8          # victim ısınması (daemon temiz baseline görsün)
COOLDOWN=12       # senaryolar arası

STREAM_BIN="$SCRIPT_DIR/STREAM/stream_c.exe"
[ -x "$STREAM_BIN" ] || STREAM_BIN="$HOME/cloud2/STREAM/stream_c.exe"

RESULTS_TXT="$SCRIPT_DIR/paper_results.txt"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'

# --- sudo ---
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}[-] sudo gerekli: sudo bash run_scenarios.sh${NC}"; exit 1
fi

# --- Dashboard ayakta mı? ---
if ! "$PYTHON_BIN" -c "import urllib.request;urllib.request.urlopen('$DASHBOARD/api/stats',timeout=5)" >/dev/null 2>&1; then
    echo -e "${RED}[-] Dashboard yanıt vermiyor: $DASHBOARD${NC}"
    echo "    Önce BAŞKA iki terminalde başlat:"
    echo "       python3 app.py"
    echo "       sudo python3 live_daemon2.py"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/collect_results.py" ]; then
    echo -e "${RED}[-] collect_results.py yok (aynı klasörde olmalı).${NC}"; exit 1
fi
if [ ! -x "$STREAM_BIN" ]; then
    echo -e "${YELLOW}[!] STREAM binary yok: $STREAM_BIN${NC}"
fi

# --- Sadece attacker'ları öldür (victim çalışmaya devam etsin) ---
kill_attackers() {
    pkill -9 -f "stream_c"   2>/dev/null
    pkill -9 -f "stress-ng"  2>/dev/null
}
# --- Her şeyi öldür ---
kill_all() {
    pkill -9 -f "mcf inp"          2>/dev/null
    pkill -9 -f "stream_c"         2>/dev/null
    pkill -9 -f "redis-benchmark"  2>/dev/null
    pkill -9 -f "stress-ng"        2>/dev/null
    sleep 1
}

trap 'echo; echo -e "${YELLOW}[*] İptal — temizleniyor...${NC}"; kill_all; exit 130' INT TERM

echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor >/dev/null 2>&1 || true

run_scenario() {
    local label="$1"; local victim_cmd="$2"; local attacker_cmd="$3"
    echo ""
    echo -e "${CYAN}========================================================${NC}"
    echo -e "${CYAN}  SENARYO: $label   (${REPS} tekrar)${NC}"
    echo -e "${CYAN}========================================================${NC}"
    kill_all
    local start; start="$(date -u +%Y-%m-%dT%H:%M:%S)"

    echo -e "${GREEN}[*] Victim başlatılıyor (core 0), ${WARMUP}s ısınma...${NC}"
    bash -c "$victim_cmd" &
    sleep "$WARMUP"

    local rep
    for rep in $(seq 1 "$REPS"); do
        echo -e "${GREEN}[*] [$label] rep $rep/$REPS — attacker AÇ (core 1)${NC}"
        bash -c "$attacker_cmd" &
        sleep "$ATTACK_WAIT"
        echo -e "${YELLOW}[*] [$label] rep $rep/$REPS — attacker KAPAT (release bekleniyor)${NC}"
        kill_attackers
        sleep "$RELEASE_WAIT"
    done

    echo -e "${GREEN}[*] Victim durduruluyor...${NC}"
    kill_all
    sleep 3

    echo -e "${GREEN}[*] Sonuçlar toplanıyor...${NC}"
    "$PYTHON_BIN" "$SCRIPT_DIR/collect_results.py" "$label" "$start"
    sleep "$COOLDOWN"
}

# --- Workload komutları ---
MCF_VICTIM='while true; do numactl --physcpubind=0 mcf inp.in >/dev/null 2>&1; done'
REDIS_VICTIM='while true; do numactl --physcpubind=0 redis-benchmark -t set,get -n 100000000 -q >/dev/null 2>&1; done'
STREAM_ATT="while true; do numactl --physcpubind=1 '$STREAM_BIN' >/dev/null 2>&1; done"
STRESS_ATT='numactl --physcpubind=1 stress-ng --matrix 1 --cache 1 >/dev/null 2>&1'

{
  echo "Noisy-Neighbor — Paper Results (v2, REPS=$REPS)"
  echo "Tarih: $(date)"
  echo "Ayar: samples_per_phase=7, policy=strangulate, ATTACK_WAIT=${ATTACK_WAIT}s"
} > "$RESULTS_TXT"

run_scenario "mcf_vs_STREAM"     "$MCF_VICTIM"   "$STREAM_ATT"
run_scenario "mcf_vs_stress-ng"  "$MCF_VICTIM"   "$STRESS_ATT"
run_scenario "redis_vs_STREAM"   "$REDIS_VICTIM" "$STREAM_ATT"

kill_all
echo ""
echo -e "${GREEN}========================================================${NC}"
echo -e "${GREEN}  TÜM SENARYOLAR BİTTİ${NC}"
echo -e "${GREEN}========================================================${NC}"
echo ""
cat "$RESULTS_TXT"
echo ""
echo -e "${CYAN}>>> paper_results.txt + paper_results_*.json dosyalarını Kazım'a at.${NC}"
