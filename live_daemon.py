"""
Live Closed-Loop Detection & Mitigation Daemon
================================================

Bu daemon her saniye sistemdeki performans sayaçlarını okur, makine
öğrenmesi modeline danışır, hysteresis filtresinden geçirir ve
gerekiyorsa Intel CAT/MBA ile otomatik mitigation uygular.

ÖNCEKİ HATALAR:
  1. Hysteresis yoktu (anlık alarm → anlık mitigation → 10s sleep
     → tekrar alarm → oscillation)
  2. Aggressor core hardcoded "1" idi (dinamik bulma yoktu)
  3. Mitigation verification yoktu (etkili mi bilmiyorduk)
  4. dashboard.py dosyası live_daemon.py'nin %95 kopyasıydı
     (artık tek dosya: bu)
  5. Scaling math gereksizdi (1s ölçüp /10 ile çarpıp tekrar *10
     yapıyorduk)

YENİ TASARIM:
  - Hysteresis: K=3 ardışık alarm → tetikle, M=10 ardışık temiz → kaldır
  - Dynamic aggressor: hem core 0 hem core 1 izle, find_aggressor_core ile bul
  - Mitigation verification: before/after IPC karşılaştır, recovery raporla
  - Tek dosyada Socket.IO entegrasyonu (dashboard.py silinebilir)
  - 100ms ölçüm (1s yerine), eğitim verisiyle uyumlu

İŞ AKIŞI:
  while True:
      core0_metrics = perf -C 0 (100ms)
      core1_metrics = perf -C 1 (100ms)
      imc_metrics  = perf -a uncore_imc (100ms)
      victim_features = engineer(victim_metrics + imc)
      prediction = model.predict(victim_features)
      hysteresis.update(prediction)
      if hysteresis.should_trigger():
          aggressor = find_aggressor_core({0: core0, 1: core1})
          verifier.snapshot_before()
          apply_mitigation(aggressor)
          time.sleep(3)  # stabilize
          result = verifier.snapshot_after_and_compare()
          dashboard.emit(result)
      if hysteresis.should_release():
          reset_all_mitigation()
"""

import time
import subprocess
import sys
import os
import joblib
import pandas as pd
import numpy as np

# --- Yerel modüller ---
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
SAMPLE_INTERVAL_MS = 100      # perf ölçüm aralığı
DAEMON_LOOP_SLEEP = 0.1        # döngü gecikmesi

HYSTERESIS_TRIGGER = 3         # K: kaç ardışık alarm ile tetiklen
HYSTERESIS_RELEASE = 10        # M: kaç ardışık temiz ile geri al

ROLLING_WINDOW = 50            # feature engineering rolling window

VICTIM_CORE = 0
ATTACKER_CORES = [1]           # potansiyel aggressor adayları (genişletilebilir)
ALL_CORES = [VICTIM_CORE] + ATTACKER_CORES

MODEL_FILE = "rf_model.pkl"
BASELINE_FILE = "baseline_ipc.pkl"  # opsiyonel (varsa kullan)

DASHBOARD_URL = os.environ.get('DASHBOARD_URL', 'http://localhost:5000')

# Verification: kaç saniye sonra ölçüm
VERIFICATION_DELAY = 3.0


# ============================================================
# Dashboard Socket.IO Client (opsiyonel)
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
        print("    Headless modda devam ediliyor.")
        _dashboard_connected = False
except ImportError:
    print("[!] python-socketio kurulu değil, dashboard olmadan devam.")
    _sio = None
    _dashboard_connected = False


def dashboard_emit(event, payload):
    """Dashboard'a event gönder (hata olursa görmezden gel)."""
    if not _dashboard_connected or _sio is None:
        return
    try:
        _sio.emit(event, payload)
    except Exception:
        pass


# ============================================================
# Hysteresis State Machine
# ============================================================
class HysteresisFilter:
    """
    K-of-M filtre: K ardışık 'attack' ile tetiklen, M ardışık 'safe'
    ile geri al. Oscillation'ı önler.
    """

    def __init__(self, k_trigger=3, m_release=10):
        self.k = k_trigger
        self.m = m_release
        self.interference_count = 0
        self.recovery_count = 0
        self.is_active = False

    def update(self, prediction):
        """
        Yeni tahmin gel: 1 (attack) veya 0 (safe).

        Returns:
            str: 'trigger', 'release', 'maintain'
                 'trigger' = mitigation şimdi tetiklenmeli
                 'release' = mitigation şimdi kaldırılmalı
                 'maintain' = mevcut durumu koru
        """
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

    def reset(self):
        self.interference_count = 0
        self.recovery_count = 0
        self.is_active = False


# ============================================================
# Perf Ölçüm (per-core)
# ============================================================
def perf_core_snapshot(core_id, duration_ms=SAMPLE_INTERVAL_MS):
    """
    Belirli bir core için tek bir perf ölçümü yap (100ms).

    Returns:
        dict: {cycles, instructions, LLC-loads, LLC-load-misses}
              Veya {} (başarısızsa)
    """
    duration_sec = duration_ms / 1000.0
    cmd = [
        "sudo", "perf", "stat", "-x", ",",
        "-C", str(core_id),
        "-e", "cycles,instructions,LLC-loads,LLC-load-misses",
        "sleep", str(duration_sec),
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=duration_sec + 5,
        )
        output = result.stderr  # perf stdout'a değil stderr'e basar
    except subprocess.TimeoutExpired:
        return {}

    metrics = {
        'cycles': 1.0,
        'instructions': 0.0,
        'LLC-loads': 1.0,
        'LLC-load-misses': 0.0,
    }
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 3:
            continue
        try:
            val = float(parts[0].replace('<not counted>', '0').replace('<not supported>', '0'))
            event = parts[2] if len(parts) > 2 else ''
            if event in metrics:
                metrics[event] = val
        except (ValueError, IndexError):
            continue
    return metrics


def perf_uncore_snapshot(duration_ms=SAMPLE_INTERVAL_MS):
    """
    Sistem geneli uncore IMC ölçümü.

    Returns:
        float: toplam cas_count (tüm 6 kanal toplamı, ham byte cinsinden değil
               CAS sayısı olarak — her CAS = 64 byte)
    """
    duration_sec = duration_ms / 1000.0
    cmd = [
        "sudo", "perf", "stat", "-x", ",", "-a",
        "-e",
        "uncore_imc_0/cas_count_read/,uncore_imc_0/cas_count_write/,"
        "uncore_imc_1/cas_count_read/,uncore_imc_1/cas_count_write/,"
        "uncore_imc_2/cas_count_read/,uncore_imc_2/cas_count_write/,"
        "uncore_imc_3/cas_count_read/,uncore_imc_3/cas_count_write/,"
        "uncore_imc_4/cas_count_read/,uncore_imc_4/cas_count_write/,"
        "uncore_imc_5/cas_count_read/,uncore_imc_5/cas_count_write/",
        "sleep", str(duration_sec),
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=duration_sec + 5,
        )
        output = result.stderr
    except subprocess.TimeoutExpired:
        return 0.0

    total_cas = 0.0
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 3:
            continue
        try:
            val = float(parts[0].replace('<not counted>', '0'))
            unit = parts[1] if len(parts) > 1 else ''
            event = parts[2] if len(parts) > 2 else ''

            # perf otomatik scale'i geri al
            if 'GiB' in unit:
                val = (val * 1073741824) / 64
            elif 'MiB' in unit:
                val = (val * 1048576) / 64
            elif 'KiB' in unit:
                val = (val * 1024) / 64
            elif unit in ('B', 'Bytes'):
                val = val / 64

            if 'cas_count' in event:
                total_cas += val
        except (ValueError, IndexError):
            continue
    return total_cas


def cas_to_mb_per_sec(cas_count, duration_ms=SAMPLE_INTERVAL_MS):
    """CAS count'u MB/s'e çevir. (Her CAS = 64 byte)."""
    bytes_in_window = cas_count * 64
    mb_in_window = bytes_in_window / 1048576
    sec_in_window = duration_ms / 1000.0
    return mb_in_window / sec_in_window if sec_in_window > 0 else 0.0


# ============================================================
# Sample Build (per-core + uncore)
# ============================================================
def observe_one_sample():
    """
    Tüm core'lar + uncore için bir snapshot al.

    Returns:
        dict: {
            'cores': {0: {...}, 1: {...}},
            'uncore_cas': float,
            'mb_per_sec': float,
            'victim_features': dict (model'e verilecek)
        }
    """
    # Ölçümler — paralel olabilirdi ama subprocess kontrol için seri
    core_metrics = {}
    for c in ALL_CORES:
        core_metrics[c] = perf_core_snapshot(c)

    uncore_cas = perf_uncore_snapshot()
    mb_per_sec = cas_to_mb_per_sec(uncore_cas)

    # Victim core (core 0) feature'larını model formatına dönüştür
    v = core_metrics[VICTIM_CORE]
    cycles = max(v.get('cycles', 1), 1)
    instructions = v.get('instructions', 0)
    llc_loads = max(v.get('LLC-loads', 1), 1)
    llc_misses = v.get('LLC-load-misses', 0)

    ipc = instructions / cycles
    llc_intensity = llc_loads / cycles
    miss_rate = llc_misses / llc_loads
    cache_dominance = llc_intensity * (1 - miss_rate)

    victim_features = {
        'IPC': ipc,
        'Total_MB_per_sec': mb_per_sec,
        'cache_dominance_score': cache_dominance,
        'LLC-load-misses': llc_misses,
        'LLC-loads': llc_loads,
    }

    # Core 1 (potansiyel aggressor) için de aynı şekilde aggressor_score için
    for core_id, m in core_metrics.items():
        m['cas_count_total'] = uncore_cas / len(ALL_CORES)  # eşit pay (kabaca)

    return {
        'cores': core_metrics,
        'uncore_cas': uncore_cas,
        'mb_per_sec': mb_per_sec,
        'victim_features': victim_features,
    }


# ============================================================
# Ana Daemon Döngüsü
# ============================================================
def main():
    print("=" * 70)
    print("  Noisy-Neighbor Live Daemon (Fixed)")
    print("  Hysteresis + Dynamic Aggressor + Verification")
    print("=" * 70)

    # 1. Model yükle
    if not os.path.exists(MODEL_FILE):
        print(f"[-] Model bulunamadı: {MODEL_FILE}")
        print("    Önce 'python train_model.py' çalıştırın.")
        sys.exit(1)

    print(f"[*] Model yükleniyor: {MODEL_FILE}")
    model = joblib.load(MODEL_FILE)

    # 2. Bileşenleri kur
    engineer = FeatureEngineer(window=ROLLING_WINDOW)
    hysteresis = HysteresisFilter(
        k_trigger=HYSTERESIS_TRIGGER,
        m_release=HYSTERESIS_RELEASE,
    )
    verifier = MitigationVerifier(
        observer=lambda: observe_one_sample()['victim_features'],
        samples_per_phase=3,
        sample_interval=1.0,
    )

    print(f"[*] Hysteresis: trigger={HYSTERESIS_TRIGGER}, "
          f"release={HYSTERESIS_RELEASE}")
    print(f"[*] Daemon başlatılıyor. Ctrl+C ile durdur.")
    print("=" * 70)

    sample_count = 0
    try:
        while True:
            sample_count += 1
            obs = observe_one_sample()
            features = obs['victim_features']

            # Rolling window'a ekle
            engineer.update(features)

            # Model inference
            X = pd.DataFrame([{f: features[f] for f in BASE_FEATURES}])
            prediction = int(model.predict(X)[0])
            try:
                confidence = float(model.predict_proba(X)[0][1])
            except Exception:
                confidence = 0.0

            # Hysteresis kararı
            decision = hysteresis.update(prediction)

            # Status string
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

            # Dashboard'a anlık veri gönder
            dashboard_emit('telemetry_update', {
                'ipc': features['IPC'],
                'mbps': features['Total_MB_per_sec'],
                'llc_misses': features['LLC-load-misses'],
                'is_attack': prediction,
                'confidence': confidence,
                'mitigation_active': hysteresis.is_active,
                'sample_count': sample_count,
            })

            # Mitigation tetikleme
            if decision == 'trigger':
                print(f"\n[!!] NOISY-NEIGHBOR ONAYLANDI "
                      f"({HYSTERESIS_TRIGGER} ardışık alarm)")

                # 1. Aggressor'ı bul (dinamik!)
                aggressor = find_aggressor_core(obs['cores'])
                if aggressor is None or aggressor == VICTIM_CORE:
                    print("[!] Aggressor tespit edilemedi, default core 1 kullanılıyor.")
                    aggressor = 1

                print(f"[*] Tespit edilen aggressor: core {aggressor}")
                dashboard_emit('mitigation_starting', {'aggressor_core': aggressor})

                # 2. Before snapshot
                print("[*] Before snapshot alınıyor...")
                verifier.reset()
                verifier.snapshot_before()

                # 3. Mitigation uygula
                print("[*] Mitigation uygulanıyor (isolate)...")
                mitigation_shield.apply_policy(
                    core_id=aggressor,
                    policy_name='isolate',
                    victim_core=VICTIM_CORE,
                )

                # 4. Stabilize bekle
                print(f"[*] {VERIFICATION_DELAY}s stabilize bekleniyor...")
                time.sleep(VERIFICATION_DELAY)

                # 5. After snapshot + karşılaştırma
                result = verifier.snapshot_after_and_compare()
                dashboard_emit('mitigation_result', result)

            elif decision == 'release':
                print(f"\n[*] Sistem {HYSTERESIS_RELEASE} ardışık örnektir temiz "
                      "— mitigation kaldırılıyor.")
                mitigation_shield.reset_all()
                dashboard_emit('mitigation_released', {})

            time.sleep(DAEMON_LOOP_SLEEP)

    except KeyboardInterrupt:
        print("\n\n[*] Kapatılıyor...")
        if hysteresis.is_active:
            print("[*] Aktif mitigation'lar geri alınıyor...")
            mitigation_shield.reset_all()
        if _sio is not None and _dashboard_connected:
            try:
                _sio.disconnect()
            except Exception:
                pass
        print("[+] Daemon kapatıldı.")


if __name__ == "__main__":
    main()
