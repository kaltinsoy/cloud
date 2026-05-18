"""
Noisy-Neighbor Detection Model Training
========================================

Bu script master_dataset.csv'yi okur, IPC drop tabanlı etiketleme yapar,
Random Forest classifier eğitir ve değerlendirir.

ÖNCEKİ HATA: 'attacker != none → label = 1' yaklaşımı label leakage
yaratıyordu. Model trivially "attacker var mı yok mu" öğreniyordu,
gerçek interference detection yapmıyordu (F1=1.00 = kırmızı bayrak).

YENİ YAKLAŞIM: Her victim workload'unun solo (attacker=none) baseline
IPC'sini hesapla, co-located run'larda IPC %20+ düşüş varsa
interference (label=1), aksi takdirde normal (label=0).

Bu sayede bazı co-located run'lar da "normal" çıkacak (interference
zayıfsa) ve model gerçek bir karar problemi öğrenecek.
"""

import pandas as pd
import numpy as np
import glob
import joblib
from pathlib import Path
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    classification_report, confusion_matrix, f1_score,
    precision_score, recall_score, roc_auc_score
)

# ============================================================
# AYARLAR
# ============================================================
IPC_DROP_THRESHOLD = 0.20           # %20 IPC düşüşü = interference
TEST_SIZE = 0.20                    # 80/20 split
RANDOM_SEED = 42
N_ESTIMATORS = 100
MAX_DEPTH = 12
OUTPUT_MODEL = "rf_model.pkl"
OUTPUT_BASELINES = "baseline_ipc.pkl"
OUTPUT_DATASET = "master_dataset.csv"


# ============================================================
# VERİ YÜKLEME
# ============================================================
def load_all_csvs():
    """Tüm data_*.csv dosyalarını birleştir."""
    all_files = sorted(glob.glob("data_*_vs_*_run*.csv"))
    if not all_files:
        raise FileNotFoundError(
            "Hiç data CSV dosyası bulunamadı. Önce run_experiments.sh çalıştırın."
        )

    print(f"[*] {len(all_files)} CSV dosyası bulundu, birleştiriliyor...")
    df_list = []
    for f in all_files:
        try:
            df = pd.read_csv(f)
            if len(df) == 0:
                print(f"    UYARI: {f} boş, atlanıyor.")
                continue
            df_list.append(df)
        except Exception as e:
            print(f"    HATA: {f} okunamadı: {e}")

    if not df_list:
        raise ValueError("Hiçbir CSV başarıyla okunamadı.")

    master_df = pd.concat(df_list, ignore_index=True)
    master_df = master_df.dropna()

    # Sonsuz/NaN feature değerlerini temizle
    master_df = master_df.replace([np.inf, -np.inf], np.nan).dropna()

    print(f"[+] Birleştirme tamamlandı: {len(master_df)} toplam sample.")
    print(f"[+] Sample dağılımı (victim, attacker):")
    print(master_df.groupby(['victim', 'attacker']).size().to_string())

    return master_df


# ============================================================
# BASELINE IPC HESAPLAMA
# ============================================================
def compute_baselines(df):
    """
    Her victim workload için solo (attacker=none) baseline IPC'sini hesapla.

    Returns:
        dict: {victim_name: median_ipc}
    """
    print("\n[*] Baseline IPC hesaplanıyor (her victim için solo IPC ortancası)...")

    solo_runs = df[df['attacker'] == 'none']
    if len(solo_runs) == 0:
        raise ValueError(
            "Hiç solo run bulunamadı! En az bir 'attacker=none' run gerekli. "
            "run_experiments.sh'de ATTACKERS=(\"none\" ...) olduğundan emin olun."
        )

    baselines = {}
    for victim, group in solo_runs.groupby('victim'):
        # Median daha robust (outlier'lara dirençli)
        baseline_ipc = group['IPC'].median()
        baselines[victim] = baseline_ipc
        print(f"    {victim:20s} baseline IPC = {baseline_ipc:.4f} "
              f"(n={len(group)}, std={group['IPC'].std():.4f})")

    return baselines


# ============================================================
# ETİKETLEME (IPC drop tabanlı)
# ============================================================
def label_samples(df, baselines, threshold=IPC_DROP_THRESHOLD):
    """
    Her sample için: IPC drop > threshold ise label=1 (interference),
    aksi takdirde label=0 (normal).

    Solo run'lar (attacker=none) her zaman label=0 olur.
    """
    print(f"\n[*] Etiketleme (IPC drop > {threshold*100:.0f}% → interference)...")

    def compute_label(row):
        # Solo run = normal her zaman
        if row['attacker'] == 'none':
            return 0

        baseline = baselines.get(row['victim'])
        if baseline is None or baseline <= 0:
            return 0  # baseline yoksa default normal

        ipc_drop = (baseline - row['IPC']) / baseline
        return 1 if ipc_drop > threshold else 0

    df = df.copy()
    df['ipc_drop_ratio'] = df.apply(
        lambda r: (baselines.get(r['victim'], r['IPC']) - r['IPC'])
                  / max(baselines.get(r['victim'], 1.0), 1e-9),
        axis=1
    )
    df['is_under_attack'] = df.apply(compute_label, axis=1)

    # Sınıf dağılımını raporla
    class_counts = df['is_under_attack'].value_counts().sort_index()
    total = len(df)
    print(f"    Normal (0):       {class_counts.get(0, 0):>6d} ({class_counts.get(0, 0)/total*100:.1f}%)")
    print(f"    Interference (1): {class_counts.get(1, 0):>6d} ({class_counts.get(1, 0)/total*100:.1f}%)")

    if class_counts.get(1, 0) == 0:
        raise ValueError(
            "Hiç interference sample'ı yok! IPC_DROP_THRESHOLD çok yüksek olabilir. "
            f"Mevcut threshold: {threshold}. Daha düşük bir değer deneyin (örn. 0.10)."
        )
    if class_counts.get(0, 0) == 0:
        raise ValueError(
            "Hiç normal sample'ı yok! Tüm sample'lar interference olarak etiketlendi. "
            "Solo run'larınız (attacker=none) var mı?"
        )

    return df


# ============================================================
# MODEL EĞİTİMİ
# ============================================================
def train_and_evaluate(df, feature_columns):
    """Random Forest + Logistic Regression baseline eğit ve karşılaştır."""

    X = df[feature_columns].values
    y = df['is_under_attack'].values

    # Stratified split (sınıf dengesini korur)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y,
        test_size=TEST_SIZE,
        random_state=RANDOM_SEED,
        stratify=y
    )
    print(f"\n[*] Train/Test split: {len(X_train)} eğitim, {len(X_test)} test")

    # --- Logistic Regression (baseline) ---
    print("\n[*] BASELINE: Logistic Regression eğitiliyor...")
    lr = LogisticRegression(max_iter=1000, random_state=RANDOM_SEED)
    lr.fit(X_train, y_train)
    lr_pred = lr.predict(X_test)
    lr_f1 = f1_score(y_test, lr_pred)
    print(f"    Logistic Regression F1 = {lr_f1:.4f}")

    # --- Random Forest (primary) ---
    print("\n[*] PRIMARY: Random Forest eğitiliyor...")
    rf = RandomForestClassifier(
        n_estimators=N_ESTIMATORS,
        max_depth=MAX_DEPTH,
        min_samples_split=5,
        random_state=RANDOM_SEED,
        n_jobs=-1,
        class_weight='balanced'  # Sınıf dengesizliği için
    )
    rf.fit(X_train, y_train)

    rf_pred = rf.predict(X_test)
    rf_proba = rf.predict_proba(X_test)[:, 1]

    rf_f1 = f1_score(y_test, rf_pred)
    rf_precision = precision_score(y_test, rf_pred)
    rf_recall = recall_score(y_test, rf_pred)
    rf_auc = roc_auc_score(y_test, rf_proba)

    print(f"\n[+] Random Forest Metrikleri:")
    print(f"    F1:        {rf_f1:.4f}")
    print(f"    Precision: {rf_precision:.4f}")
    print(f"    Recall:    {rf_recall:.4f}")
    print(f"    ROC-AUC:   {rf_auc:.4f}")

    # F1=1.0 uyarısı (label leakage işareti)
    if rf_f1 > 0.99:
        print("\n[!] UYARI: F1 > 0.99 — label leakage olabilir!")
        print("    Akademik paper'da bu kırmızı bayrak. Feature'ları kontrol et.")
    elif rf_f1 < 0.70:
        print("\n[!] UYARI: F1 < 0.70 — model yeterince iyi öğrenemedi.")
        print("    Daha fazla veri veya daha iyi feature gerekiyor olabilir.")
    else:
        print("\n[✓] F1 sağlıklı aralıkta (0.70-0.99). Gerçekçi model.")

    print(f"\n[+] Confusion Matrix:")
    cm = confusion_matrix(y_test, rf_pred)
    print(f"                  Predicted Normal   Predicted Attack")
    print(f"    Actual Normal     {cm[0][0]:>6d}              {cm[0][1]:>6d}")
    print(f"    Actual Attack     {cm[1][0]:>6d}              {cm[1][1]:>6d}")

    print(f"\n[+] Detaylı Rapor:")
    print(classification_report(y_test, rf_pred,
                                target_names=['Normal', 'Interference'],
                                digits=4))

    # Cross-validation (overfitting kontrolü)
    print("\n[*] 5-fold Cross-Validation çalıştırılıyor...")
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=RANDOM_SEED)
    cv_scores = cross_val_score(rf, X, y, cv=cv, scoring='f1', n_jobs=-1)
    print(f"    CV F1 ortalama: {cv_scores.mean():.4f} (± {cv_scores.std():.4f})")

    if abs(rf_f1 - cv_scores.mean()) > 0.05:
        print("[!] UYARI: Test F1 ile CV F1 arasında büyük fark var (overfitting riski).")

    # Feature importance
    print("\n[+] Feature Importance:")
    importances = sorted(
        zip(feature_columns, rf.feature_importances_),
        key=lambda x: x[1],
        reverse=True
    )
    for name, imp in importances:
        bar = '█' * int(imp * 50)
        print(f"    {name:25s} {imp*100:>5.2f}%  {bar}")

    return rf, lr, {
        'f1': rf_f1,
        'precision': rf_precision,
        'recall': rf_recall,
        'auc': rf_auc,
        'cv_f1_mean': cv_scores.mean(),
        'cv_f1_std': cv_scores.std(),
        'lr_baseline_f1': lr_f1,
    }


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 70)
    print("  Noisy-Neighbor Detection Model Training")
    print("  IPC-Drop Based Labeling (Fixed)")
    print("=" * 70)

    # 1. Veri yükle
    df = load_all_csvs()
    df.to_csv(OUTPUT_DATASET, index=False)
    print(f"[+] Master dataset kaydedildi: {OUTPUT_DATASET}")

    # 2. Baseline IPC hesapla
    baselines = compute_baselines(df)
    joblib.dump(baselines, OUTPUT_BASELINES)
    print(f"\n[+] Baseline IPC değerleri kaydedildi: {OUTPUT_BASELINES}")

    # 3. Etiketle
    df = label_samples(df, baselines)

    # 4. Feature seç (label-leakage'ı önle: ipc_drop_ratio'yu feature olarak verme!)
    feature_columns = [
        'IPC',
        'Total_MB_per_sec',
        'cache_dominance_score',
        'LLC-load-misses',
        'LLC-loads',
    ]

    # Sadece mevcut feature'ları al
    feature_columns = [c for c in feature_columns if c in df.columns]
    print(f"\n[*] Kullanılacak {len(feature_columns)} feature: {feature_columns}")

    # 5. Eğit + değerlendir
    model, baseline_model, metrics = train_and_evaluate(df, feature_columns)

    # 6. Modeli kaydet
    joblib.dump(model, OUTPUT_MODEL)
    print(f"\n[+] Model kaydedildi: {OUTPUT_MODEL}")
    print(f"[+] Baseline IPC'leri kaydedildi: {OUTPUT_BASELINES}")

    # 7. Özet
    print("\n" + "=" * 70)
    print("  EĞİTİM TAMAMLANDI")
    print("=" * 70)
    print(f"  Random Forest F1:    {metrics['f1']:.4f}")
    print(f"  CV F1 (5-fold):      {metrics['cv_f1_mean']:.4f} (± {metrics['cv_f1_std']:.4f})")
    print(f"  LR baseline F1:      {metrics['lr_baseline_f1']:.4f}")
    print(f"  Precision:           {metrics['precision']:.4f}")
    print(f"  Recall:              {metrics['recall']:.4f}")
    print(f"  ROC-AUC:             {metrics['auc']:.4f}")
    print("=" * 70)


if __name__ == "__main__":
    main()
