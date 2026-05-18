"""
Live Closed-Loop Detection & Mitigation Daemon (v3 - Concurrency & Scale Fix)
=============================================================================
"""

import time
import subprocess
import sys
import os
import joblib
import pandas as pd
import numpy as np

from feature_engineering import (
    FeatureEngineer,
    find_aggressor_core,
    BASE_FEATURES,
)
import mitigation_shield
from mitigation_verification import MitigationVerifier

# ============================================================
# AYARLAR
# ============================================================
SAMPLE_INTERVAL_SEC = 1.0      # 1 saniyelik stabil ölçüm (perf overhead'ini yutar)
SCALE_FACTOR = 10.0            # Eğitim 100ms (0.1sn) idi, değerleri 10'a böl

HYSTERESIS_TRIGGER = 3         # K: kaç ardışık alarm ile tetiklen
HYSTERESIS_RELEASE = 10        # M: kaç ardışık temiz ile geri al
ROLLING_WINDOW = 50            # feature engineering rolling window

VICTIM_CORE = 0
ATTACKER_CORE = 1
ALL_CORES = [VICTIM_CORE, ATTACKER_CORE]

MODEL_FILE = "rf_model.pkl"
DASHBOARD_URL = os.environ.get('DASHBOARD_URL', 'http://localhost:5000')
VERIFICATION_DELAY = 3.0

UNCORE_EVENTS = (
    "uncore_imc_0/cas_count_read/,uncore_imc_0/cas_count_write/,"
    "uncore_imc_1/cas_count_read/,uncore_imc_1/cas_count_write/,"
    "uncore_imc_2/cas_count_read/,uncore_imc_2/cas_count_write/,"
    "uncore_imc_3/cas_count_read/,uncore_imc_3/cas_count_write/,"
    "uncore_imc_4/cas_count_read/,uncore_imc_4/cas_count_write/,"
    "uncore_imc_5/cas_count_read/,uncore_imc_5/cas_count_write/"
)

# ============================================================
# Dashboard Socket.IO Client
# ============================================================
try:
    import socketio
    _sio = socketio.Client(reconnection=True, reconnection_attempts=2)
    try:
        _sio.connect(DASHBOARD_URL, wait_timeout=3)
        print(f"[+] Dashboard'a bağlandı: {DASHBOARD_URL}")
        _dashboard_connected = True
    except Exception as e:
        print(f"[!] Dashboard bağlantısı kurulamadı: {e}")
        _dashboard_connected = False
except ImportError:
    print("[!] python-socketio kurulu değil, dashboard olmadan devam.")
    _sio = None
    _dashboard_connected = False

def dashboard_emit(event, payload):
    if not _dashboard_connected or _sio is None: return
    try: _sio.emit(event, payload)
    except: pass

# ============================================================
# Hysteresis State Machine
# ============================================================
class HysteresisFilter:
    def __init__(self, k_trigger=3, m_release=10):
        self.k = k_trigger
        self.m = m_release
        self.interference_count = 0
        self.recovery_count = 0
        self.is_active = False

    def update(self, prediction):
        if prediction == 1:
            self.interference_count += 1
            self.recovery_count = 0
            if self.interference_count >= self.k and not self.is_active:
                self.is_active = True
                return 'trigger'
        else:
            self.recovery_count += 1
            self.interference_count = 0
            if self.recovery_count >= self.m and self.is_active:
                self.is_active = False
                return 'release'
        return 'maintain'

# ============================================================
# Perf Ölçüm & Parse (Concurrent & Scaled)
# ============================================================
def parse_perf_output(output, is_uncore=False):
    metrics = {}
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 3: continue
        try:
            val_str = parts[0].replace('<not counted>', '0').replace('<not supported>', '0')
            val = float(val_str)
            event = parts[2] if len(parts) > 2 else ''
            
            if is_uncore:
                unit = parts[1] if len(parts) > 1 else ''
                if 'GiB' in unit: val = (val * 1073741824) / 64
                elif 'MiB' in unit: val = (val * 1048576) / 64
                elif 'KiB' in unit: val = (val * 1024) / 64
                elif unit in ('B', 'Bytes'): val = val / 64
                
                if 'cas_count' in event:
                    metrics['cas_count_total'] = metrics.get('cas_count_total', 0.0) + val
            else:
                metrics[event] = val
        except:
            pass
    return metrics

def observe_one_sample():
    cmd_v = ["sudo", "perf", "stat", "-x", ",", "-C", str(VICTIM_CORE), "-e", "cycles,instructions,LLC-loads,LLC-load-misses", "sleep", str(SAMPLE_INTERVAL_SEC)]
    cmd_a = ["sudo", "perf", "stat", "-x", ",", "-C", str(ATTACKER_CORE), "-e", "cycles,instructions,LLC-loads,LLC-load-misses", "sleep", str(SAMPLE_INTERVAL_SEC)]
    cmd_u = ["sudo", "perf", "stat", "-x", ",", "-a", "-e", UNCORE_EVENTS, "sleep", str(SAMPLE_INTERVAL_SEC)]
    
    # EŞZAMANLI (Concurrent) BAŞLATMA
    p_v = subprocess.Popen(cmd_v, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    p_a = subprocess.Popen(cmd_a, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    p_u = subprocess.Popen(cmd_u, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    
    _, err_v = p_v.communicate()
    _, err_a = p_a.communicate()
    _, err_u = p_u.communicate()
    
    v = parse_perf_output(err_v, is_uncore=False)
    a = parse_perf_output(err_a, is_uncore=False)
    u = parse_perf_output(err_u, is_uncore=True)
    
    # EĞİTİM VERİSİNE UYGUN ÖLÇEKLENDİRME (/10)
    cycles = max(v.get('cycles', 1) / SCALE_FACTOR, 1)
    instructions = v.get('instructions', 0) / SCALE_FACTOR
    llc_loads = max(v.get('LLC-loads', 1) / SCALE_FACTOR, 1)
    llc_misses = v.get('LLC-load-misses', 0) / SCALE_FACTOR
    
    ipc = instructions / cycles
    llc_intensity = llc_loads / cycles
    miss_rate = llc_misses / llc_loads
    cache_dominance = llc_intensity * (1 - miss_rate)
    
    raw_uncore = u.get('cas_count_total', 0)
    uncore_cas = raw_uncore / SCALE_FACTOR
    mb_per_sec = (uncore_cas * 64 / 1048576) * 10
    
    victim_features = {
        'IPC': ipc,
        'Total_MB_per_sec': mb_per_sec,
        'cache_dominance_score': cache_dominance,
        'LLC-load-misses': llc_misses,
        'LLC-loads': llc_loads,
    }
    
    a['cas_count_total'] = raw_uncore / 2.0
    v['cas_count_total'] = raw_uncore / 2.0
    
    return {
        'cores': {VICTIM_CORE: v, ATTACKER_CORE: a},
        'uncore_cas': raw_uncore,
        'mb_per_sec': mb_per_sec,
        'victim_features': victim_features,
    }

# ============================================================
# Ana Daemon Döngüsü
# ============================================================
def main():
    print("=" * 70)
    print("  Noisy-Neighbor Live Daemon (v3 - Concurrent Fix)")
    print("  Hysteresis + Dynamic Aggressor + Verification")
    print("=" * 70)

    if not os.path.exists(MODEL_FILE):
        print(f"[-] Model bulunamadı: {MODEL_FILE}")
        sys.exit(1)

    model = joblib.load(MODEL_FILE)
    engineer = FeatureEngineer(window=ROLLING_WINDOW)
    hysteresis = HysteresisFilter(k_trigger=HYSTERESIS_TRIGGER, m_release=HYSTERESIS_RELEASE)
    
    verifier = MitigationVerifier(
        observer=lambda: observe_one_sample()['victim_features'],
        samples_per_phase=3,
        sample_interval=0, # Zaten observe_one_sample 1sn sürüyor
    )

    print("[*] Daemon başlatılıyor. Gözlemler 1s aralıklarla yapılıyor...")
    print("=" * 70)

    sample_count = 0
    try:
        while True:
            sample_count += 1
            obs = observe_one_sample()
            features = obs['victim_features']

            engineer.update(features)
            
            X = pd.DataFrame([{f: features[f] for f in BASE_FEATURES}])
            prediction = int(model.predict(X)[0])
            try:
                confidence = float(model.predict_proba(X)[0][1])
            except:
                confidence = 0.0

            decision = hysteresis.update(prediction)
            
            status_symbol = '!' if prediction == 1 else '✓'
            print(
                f"\r[{sample_count:>5d}] "
                f"[{status_symbol}] "
                f"IPC={features['IPC']:.2f}  "
                f"MB/s={features['Total_MB_per_sec']:.0f}  "
                f"miss={features['LLC-load-misses']:.0f}  "
                f"conf={confidence:.2f}  "
                f"int={hysteresis.interference_count}/{HYSTERESIS_TRIGGER}  "
                f"rec={hysteresis.recovery_count}/{HYSTERESIS_RELEASE}  ",
                end='', flush=True,
            )

            dashboard_emit('telemetry_update', {
                'ipc': features['IPC'],
                'mbps': features['Total_MB_per_sec'],
                'llc_misses': features['LLC-load-misses'],
                'is_attack': prediction,
                'confidence': confidence,
                'mitigation_active': hysteresis.is_active,
                'sample_count': sample_count,
            })

            if decision == 'trigger':
                print(f"\n[!!] NOISY-NEIGHBOR ONAYLANDI ({HYSTERESIS_TRIGGER} ardışık alarm)")
                aggressor = find_aggressor_core(obs['cores'])
                if aggressor is None or aggressor == VICTIM_CORE: aggressor = ATTACKER_CORE

                print(f"[*] Tespit edilen aggressor: core {aggressor}")
                dashboard_emit('mitigation_starting', {'aggressor_core': aggressor})

                print("[*] Before snapshot alınıyor...")
                verifier.reset()
                verifier.snapshot_before()

                print("[*] Mitigation uygulanıyor (isolate)...")
                mitigation_shield.apply_policy(
                    core_id=aggressor,
                    policy_name='isolate',
                    victim_core=VICTIM_CORE,
                )

                print(f"[*] {VERIFICATION_DELAY}s stabilize bekleniyor...")
                time.sleep(VERIFICATION_DELAY)

                result = verifier.snapshot_after_and_compare()
                dashboard_emit('mitigation_result', result)

            elif decision == 'release':
                print(f"\n[*] Sistem {HYSTERESIS_RELEASE} ardışık örnektir temiz — mitigation kaldırılıyor.")
                mitigation_shield.reset_all()
                dashboard_emit('mitigation_released', {})

    except KeyboardInterrupt:
        print("\n\n[*] Kapatılıyor...")
        if hysteresis.is_active:
            print("[*] Aktif mitigation'lar geri alınıyor...")
            mitigation_shield.reset_all()
        if _sio is not None and _dashboard_connected:
            try: _sio.disconnect()
            except: pass
        print("[+] Daemon kapatıldı.")

if __name__ == "__main__":
    main()
