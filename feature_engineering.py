"""
Feature Engineering for Noisy-Neighbor Detection
==================================================

Bu modül ham perf counter'lardan türetilmiş feature'ları hesaplar:
  - Rolling window istatistikleri (mean, std)
  - Velocity (anlık değişim hızı)
  - Türetilmiş oranlar (cross-counter)
  - Cache pressure metrikleri

Hem eğitim hem inference için aynı pipeline kullanılır.

KULLANIM:
    Eğitimde:
        df = pd.read_csv("master_dataset.csv")
        df = compute_rolling_features(df, window=50)
        X = df[FEATURE_COLS]

    Inference'ta:
        engineer = FeatureEngineer(window=50)
        engineer.update(new_sample)
        features = engineer.get_features()
"""

import numpy as np
import pandas as pd
from collections import deque


# Ana feature listesi (modele verilen feature'lar)
BASE_FEATURES = [
    'IPC',
    'Total_MB_per_sec',
    'cache_dominance_score',
    'LLC-load-misses',
    'LLC-loads',
]

# Rolling window türetilmiş feature'lar
ROLLING_FEATURES = [
    'rolling_mean_IPC',
    'rolling_std_IPC',
    'rolling_mean_miss_rate',
    'rolling_mean_MB_per_sec',
    'rolling_std_MB_per_sec',
    'ipc_velocity',
    'ipc_drop_from_rolling',
]

# Aggressor identification için per-core feature'lar (telemetry.py'den)
PER_CORE_FEATURES = [
    'victim_IPC', 'victim_cache_dominance',
    'attacker_IPC', 'attacker_cache_dominance',
]

# Tam set (training için)
ALL_FEATURES = BASE_FEATURES + ROLLING_FEATURES


# ============================================================
# OFFLINE (batch) FEATURE ENGINEERING
# ============================================================
def compute_rolling_features(df, window=50, group_by=('victim', 'attacker', 'run_id')):
    """
    DataFrame üzerinde rolling window feature'ları hesapla.

    Args:
        df: master_dataset DataFrame
        window: rolling window size (sample sayısı, default 50 = 5 saniye)
        group_by: aynı experiment içinde rolling yap, run'lar karışmasın

    Returns:
        Yeni feature'lar eklenmiş DataFrame
    """
    df = df.copy()
    df = df.sort_values(list(group_by) + ['time_step']).reset_index(drop=True)

    # Grup başına rolling işlemleri uygula
    grouped = df.groupby(list(group_by))

    if 'IPC' in df.columns:
        df['rolling_mean_IPC'] = grouped['IPC'].transform(
            lambda s: s.rolling(window, min_periods=1).mean()
        )
        df['rolling_std_IPC'] = grouped['IPC'].transform(
            lambda s: s.rolling(window, min_periods=1).std().fillna(0)
        )
        # Velocity: ardışık IPC farkı
        df['ipc_velocity'] = grouped['IPC'].diff().fillna(0)

        # Drop: anlık IPC ile rolling mean arasındaki fark
        df['ipc_drop_from_rolling'] = (
            (df['rolling_mean_IPC'] - df['IPC'])
            / df['rolling_mean_IPC'].replace(0, np.nan)
        ).fillna(0)

    if 'LLC-load-misses' in df.columns and 'LLC-loads' in df.columns:
        miss_rate = (df['LLC-load-misses']
                     / df['LLC-loads'].replace(0, np.nan)).fillna(0)
        df['miss_rate'] = miss_rate
        df['rolling_mean_miss_rate'] = (
            df.groupby(list(group_by))['miss_rate'].transform(
                lambda s: s.rolling(window, min_periods=1).mean()
            )
        )

    if 'Total_MB_per_sec' in df.columns:
        df['rolling_mean_MB_per_sec'] = grouped['Total_MB_per_sec'].transform(
            lambda s: s.rolling(window, min_periods=1).mean()
        )
        df['rolling_std_MB_per_sec'] = grouped['Total_MB_per_sec'].transform(
            lambda s: s.rolling(window, min_periods=1).std().fillna(0)
        )

    # Inf/NaN temizliği
    df = df.replace([np.inf, -np.inf], np.nan)

    return df


# ============================================================
# ONLINE (streaming) FEATURE ENGINEERING
# ============================================================
class FeatureEngineer:
    """
    Real-time inference için rolling window feature engineer.

    Daemon her 100ms yeni bir sample geldiğinde update() çağırır,
    sonra get_features() ile model'e verilecek feature vektörünü alır.

    Kullanım:
        engineer = FeatureEngineer(window=50)
        engineer.update({'IPC': 1.8, 'Total_MB_per_sec': 1200, ...})
        features = engineer.get_features()  # dict
        # veya
        df = engineer.get_features_df()  # pandas DataFrame (model.predict() için)
    """

    def __init__(self, window=50):
        self.window = window
        self.history = deque(maxlen=window)

    def update(self, sample):
        """
        Yeni bir sample ekle.

        sample: dict, BASE_FEATURES içeriği
        """
        self.history.append(sample)

    def get_features(self):
        """
        Mevcut sample + rolling feature'ları döndür.

        Returns:
            dict: tüm feature'lar
        """
        if not self.history:
            return {f: 0.0 for f in ALL_FEATURES}

        current = self.history[-1]
        features = dict(current)

        # IPC rolling
        ipc_history = [s.get('IPC', 0) for s in self.history]
        if ipc_history:
            features['rolling_mean_IPC'] = float(np.mean(ipc_history))
            features['rolling_std_IPC'] = float(np.std(ipc_history))
            if len(ipc_history) >= 2:
                features['ipc_velocity'] = float(ipc_history[-1] - ipc_history[-2])
            else:
                features['ipc_velocity'] = 0.0
            rolling_ipc = features['rolling_mean_IPC']
            if rolling_ipc > 0:
                features['ipc_drop_from_rolling'] = float(
                    (rolling_ipc - current.get('IPC', 0)) / rolling_ipc
                )
            else:
                features['ipc_drop_from_rolling'] = 0.0

        # MB/s rolling
        mb_history = [s.get('Total_MB_per_sec', 0) for s in self.history]
        if mb_history:
            features['rolling_mean_MB_per_sec'] = float(np.mean(mb_history))
            features['rolling_std_MB_per_sec'] = float(np.std(mb_history))

        # Miss rate rolling
        miss_rates = []
        for s in self.history:
            loads = s.get('LLC-loads', 0)
            misses = s.get('LLC-load-misses', 0)
            if loads > 0:
                miss_rates.append(misses / loads)
            else:
                miss_rates.append(0)
        if miss_rates:
            features['rolling_mean_miss_rate'] = float(np.mean(miss_rates))

        return features

    def get_features_df(self, feature_cols=None):
        """
        Model'e verilebilecek tek-satırlık DataFrame döndür.

        feature_cols: liste, hangi feature'ların kolon olacağı (sıralı).
                      None ise BASE_FEATURES kullanılır (geri uyumluluk).
        """
        if feature_cols is None:
            feature_cols = BASE_FEATURES

        features = self.get_features()
        row = {c: features.get(c, 0.0) for c in feature_cols}
        return pd.DataFrame([row])

    def reset(self):
        """History'i temizle (örn. yeni deney başlangıcı)."""
        self.history.clear()

    def is_warmed_up(self):
        """Yeterli history birikmiş mi (rolling stats güvenilir)?"""
        return len(self.history) >= max(5, self.window // 10)


# ============================================================
# AGGRESSOR IDENTIFICATION
# ============================================================
def find_aggressor_core(core_metrics):
    """
    Multi-signal scoring ile aggressor core'u bul.

    Args:
        core_metrics: dict, {core_id: {LLC-loads, LLC-misses, cycles,
                                       cas_count_total (opsiyonel)}}

    Returns:
        int: en yüksek aggressor skoruna sahip core id
    """
    if not core_metrics:
        return None

    scores = {}
    for core_id, m in core_metrics.items():
        cycles = m.get('cycles', 0)
        llc_loads = m.get('LLC-loads', 0)
        llc_misses = m.get('LLC-load-misses', 0)
        imc_total = m.get('cas_count_total', 0)

        if cycles <= 0 or llc_loads <= 0:
            scores[core_id] = 0.0
            continue

        llc_intensity = llc_loads / cycles
        imc_intensity = imc_total / cycles
        miss_rate = llc_misses / llc_loads
        cache_dominance = 1 - miss_rate

        # Ağırlıklı skor:
        #   - Cache çok kullanan (llc_intensity) + miss yapmıyor (dominance)
        #     → aggressor (cache'te baskın)
        #   - Memory bandwidth yoğunluğu da aggressor işareti
        score = (
            llc_intensity * 0.30
            + imc_intensity * 0.40
            + cache_dominance * 0.30
        )
        scores[core_id] = score

    if not scores:
        return None

    return max(scores, key=scores.get)


if __name__ == "__main__":
    # Smoke test
    print("[*] FeatureEngineer smoke test...")
    eng = FeatureEngineer(window=10)
    for i in range(15):
        eng.update({
            'IPC': 1.8 - i * 0.05,
            'Total_MB_per_sec': 1000 + i * 100,
            'cache_dominance_score': 0.5 - i * 0.02,
            'LLC-load-misses': 200 + i * 50,
            'LLC-loads': 5000,
        })
    f = eng.get_features()
    print("Latest features:")
    for k, v in f.items():
        print(f"  {k:30s} = {v:.4f}")

    print("\n[*] Aggressor identification smoke test...")
    metrics = {
        0: {'cycles': 1e9, 'LLC-loads': 5000, 'LLC-load-misses': 200,
            'cas_count_total': 1e6},
        1: {'cycles': 1e9, 'LLC-loads': 8000, 'LLC-load-misses': 300,
            'cas_count_total': 5e6},  # daha aggressor
    }
    aggressor = find_aggressor_core(metrics)
    print(f"  Aggressor core: {aggressor}  (beklenen: 1)")
