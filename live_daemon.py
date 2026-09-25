"""
Live Closed-Loop Detection & Mitigation Daemon (v3 — REAL MITIGATION)
=====================================================================

Bu daemon her örnekte performans sayaçlarını okur, ML modeline danışır,
hysteresis filtresinden geçirir ve gerekiyorsa Intel CAT/MBA ile otomatik
mitigation uygular — ve uygulamanın GERÇEKTEN olduğunu doğrular.

v3'TE DÜZELTİLEN KRİTİK HATALAR:
  1. mitigation_shield COS tanımlıyordu ama core'u ATAMIYORDU (-a yoktu).
     → Mitigation hiç çalışmıyordu, recovery hep ~%0 çıkıyordu.
     → Artık shield hem -e (tanım) hem -a (atama) yapıyor.
  2. Daemon 'isolate' (sadece cache) kullanıyordu. STREAM bir BANDWIDTH
     saldırısı; cache partition'ı bandwidth saldırısını durdurmaz.
     → Artık 'strangulate' (cache 2-way + MBA %20) kullanıyor.
  3. Daemon apply_policy'nin başarılı olup olmadığına bakmıyordu;
     dashboard pqos başarısız olsa bile "MITIGATION ACTIVE" gösteriyordu.
     → Artık dönüş değeri + verify_association ile gerçek durum gönderiliyor.

ÖNCEKİ (v2) DÜZELTMELERİ (hâlâ geçerli):
  - Hysteresis: K=3 ardışık alarm → tetikle, M=10 ardışık temiz → kaldır
  - Dynamic aggressor: find_aggressor_core ile bul
  - Mitigation verification: before/after IPC, recovery raporla
  - 100ms ölçüm (eğitim verisiyle uyumlu)
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

# Before/after snapshot'ta kaç örnek ortalanacak.
# Daha çok örnek = daha temiz mean±std (mcf gibi salınan workload'larda kritik).
# Demo için 3'e düşürülebilir (dashboard daha az "donar"); paper verisi için 7.
VERIFY_SAMPLES_PER_PHASE = 7

# ============================================================
# MITIGATION POLICY
# ============================================================
# 'strangulate' = cache 2-way (0x003) + MBA %20 → attacker'ı hem cache hem
#   bandwidth'te boğar. Bandwidth saldırılarına (STREAM) karşı ŞART.
# 'isolate'     = sadece cache 2-way, bandwidth dokunulmaz. Sadece cache
#   kirletme saldırılarına (stress-ng --cache) karşı yeterli; STREAM'e değil.
# Tespit edilen bir aggressor'a karşı güvenli evrensel seçim: strangulate.
MITIGATION_POLICY = 'strangulate'

# --- Dürüst durum bayrakları (dashboard'a gerçek durumu göndermek için) ---
_binding_ok = False       # son mitigation core'a gerçekten bağlandı mı
_active_policy = None      # şu an uygulanan policy adı (None = yok)


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
    """Belirli bir core için tek bir perf ölçümü (100ms)."""
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
        output = result.stderr  # perf stderr'e basar
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
    """Sistem geneli uncore IMC ölçümü (6 kanal cas_count toplamı)."""
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
    """CAS count → MB/s (her CAS = 64 byte)."""
    bytes_in_window = cas_count * 64
    mb_in_window = bytes_in_window / 1048576
    sec_in_window = duration_ms / 1000.0
    return mb_in_window / sec_in_window if sec_in_window > 0 else 0.0


# ============================================================
# Sample Build (per-core + uncore)
# ============================================================
def observe_one_sample():
    """Tüm core'lar + uncore için bir snapshot al."""
    core_metrics = {}
    for c in ALL_CORES:
        core_metrics[c] = perf_core_snapshot(c)

    uncore_cas = perf_uncore_snapshot()
    mb_per_sec = cas_to_mb_per_sec(uncore_cas)

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

    for core_id, m in core_metrics.items():
        m['cas_count_total'] = uncore_cas / len(ALL_CORES)

    return {
        'cores': core_metrics,
        'uncore_cas': uncore_cas,
        'mb_per_sec': mb_per_sec,
        'victim_features': victim_features,
    }


# ============================================================
# Mitigation uygula + DOĞRULA (gerçekten bağlandı mı?)
# ============================================================
def apply_and_verify(aggressor, victim_core, policy_name):
    """
    Mitigation uygula ve gerçekten core'a bağlandığını doğrula.

    Returns:
        (applied: bool, binding_confirmed: bool)
          applied           = pqos komutları hatasız döndü mü
          binding_confirmed = pqos -s ile core→COS ataması teyit edildi mi
    """
    applied = mitigation_shield.apply_policy(
        core_id=aggressor,
        policy_name=policy_name,
        victim_core=victim_core,
    )
    # verify_association: True / False / None(parse edilemedi)
    verified = mitigation_shield.verify_association(
        aggressor, mitigation_shield.ATTACKER_COS
    )
    binding_confirmed = (verified is True)
    return applied, binding_confirmed


# ============================================================
# Ana Daemon Döngüsü
# ============================================================
def main():
    global _binding_ok, _active_policy

    print("=" * 70)
    print("  Noisy-Neighbor Live Daemon (v3 — Real Mitigation)")
    print("  Hysteresis + Dynamic Aggressor + Verified CAT/MBA")
    print("=" * 70)

    if not os.path.exists(MODEL_FILE):
        print(f"[-] Model bulunamadı: {MODEL_FILE}")
        print("    Önce 'python train_model.py' çalıştırın.")
        sys.exit(1)

    print(f"[*] Model yükleniyor: {MODEL_FILE}")
    model = joblib.load(MODEL_FILE)

    engineer = FeatureEngineer(window=ROLLING_WINDOW)
    hysteresis = HysteresisFilter(
        k_trigger=HYSTERESIS_TRIGGER,
        m_release=HYSTERESIS_RELEASE,
    )
    verifier = MitigationVerifier(
        observer=lambda: observe_one_sample()['victim_features'],
        samples_per_phase=VERIFY_SAMPLES_PER_PHASE,
        sample_interval=1.0,
    )

    print(f"[*] Hysteresis: trigger={HYSTERESIS_TRIGGER}, release={HYSTERESIS_RELEASE}")
    print(f"[*] Mitigation policy: {MITIGATION_POLICY}")

    # Önceki (çökmüş olabilecek) çalışmadan kalan mitigation'ları temizle
    print("[*] Başlangıç temizliği: core'lar baseline'a (COS0) çekiliyor...")
    for c in ALL_CORES:
        mitigation_shield.reset_core(c)

    print(f"[*] Daemon başlatılıyor. Ctrl+C ile durdur.")
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
            except Exception:
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

            # Dashboard'a anlık veri — mitigation_active artık GERÇEK durumu
            # gösteriyor (hysteresis istedi + gerçekten bağlandı).
            dashboard_emit('telemetry_update', {
                'ipc': features['IPC'],
                'mbps': features['Total_MB_per_sec'],
                'llc_misses': features['LLC-load-misses'],
                'is_attack': prediction,
                'confidence': confidence,
                'mitigation_active': hysteresis.is_active and _binding_ok,
                'binding_confirmed': _binding_ok,
                'active_policy': _active_policy,
                'sample_count': sample_count,
            })

            # --- Mitigation tetikleme ---
            if decision == 'trigger':
                print(f"\n[!!] NOISY-NEIGHBOR ONAYLANDI "
                      f"({HYSTERESIS_TRIGGER} ardışık alarm)")

                aggressor = find_aggressor_core(obs['cores'])
                if aggressor is None or aggressor == VICTIM_CORE:
                    print("[!] Aggressor tespit edilemedi, default core 1.")
                    aggressor = 1

                print(f"[*] Tespit edilen aggressor: core {aggressor}")
                dashboard_emit('mitigation_starting', {
                    'aggressor_core': aggressor,
                    'policy': MITIGATION_POLICY,
                })

                # 1. Before snapshot (mitigation uygulanmadan önce)
                print("[*] Before snapshot alınıyor...")
                verifier.reset()
                verifier.snapshot_before()

                # 2. Mitigation uygula + GERÇEKTEN bağlandı mı doğrula
                print(f"[*] Mitigation uygulanıyor ({MITIGATION_POLICY})...")
                applied, _binding_ok = apply_and_verify(
                    aggressor, VICTIM_CORE, MITIGATION_POLICY
                )
                _active_policy = MITIGATION_POLICY if (applied and _binding_ok) else None

                if applied and _binding_ok:
                    print(f"[✓] CAT/MBA core {aggressor}'e bağlandı ve teyit edildi.")
                elif applied:
                    print(f"[~] pqos hatasız döndü ama atama teyit edilemedi (pqos -s parse).")
                else:
                    print(f"[✗] MITIGATION UYGULANAMADI — pqos/RDT hatası!")

                # 3. Stabilize bekle
                print(f"[*] {VERIFICATION_DELAY}s stabilize bekleniyor...")
                time.sleep(VERIFICATION_DELAY)

                # 4. After snapshot + karşılaştırma
                result = verifier.snapshot_after_and_compare()
                result['applied'] = applied
                result['binding_confirmed'] = _binding_ok
                result['policy'] = MITIGATION_POLICY
                result['aggressor_core'] = aggressor
                dashboard_emit('mitigation_result', result)

            elif decision == 'release':
                print(f"\n[*] Sistem {HYSTERESIS_RELEASE} ardışık örnektir temiz "
                      "— mitigation kaldırılıyor.")
                mitigation_shield.reset_all()
                _binding_ok = False
                _active_policy = None
                dashboard_emit('mitigation_released', {})

            time.sleep(DAEMON_LOOP_SLEEP)

    except KeyboardInterrupt:
        print("\n\n[*] Kapatılıyor...")
        if _binding_ok or hysteresis.is_active:
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
