# Migration Notes — Eski → Yeni Proje

Bu doküman eski `cloud-main` klasöründen yeni `cloud-main-fixed`
klasörüne geçişte yapılan değişiklikleri özetler.

---

## Silinmesi Gereken Eski Dosyalar

Yeni proje yapısında bunlar **yer almaz**:

| Eski dosya | Sebep |
|---|---|
| `dashboard.py` | `live_daemon.py`'nin neredeyse kopyasıydı, ikisi birleşti |
| `live_daemon.py.backup` | Eski versiyon, gereksiz |
| `cache_dominance.py` | `feature_engineering.py`'ye taşındı |
| `estimate_cache_dominance.py` | `feature_engineering.py`'ye taşındı |
| `find_aggressor_pmu_based.py` | `feature_engineering.py`'ye taşındı |
| `telemetry2.py` | Eski deneme dosyası |

---

## Eski → Yeni Dosya Eşleştirmesi

| Eski | Yeni | Durum |
|---|---|---|
| `train_model.py` | `train_model.py` | IPC-drop labeling ile yeniden yazıldı |
| `telemetry.py` | `telemetry.py` | Per-core (0 ve 1) izleme ile yeniden yazıldı |
| `live_daemon.py` | `live_daemon.py` | Hysteresis + dynamic aggressor + verifier ile yeniden yazıldı |
| `mitigation_shield.py` | `mitigation_shield.py` | Hem CLI hem Python API olarak yeniden yazıldı |
| `app.py` | `app.py` | Event history + 4 endpoint ile genişletildi |
| `templates/index.html` | `templates/index.html` | 4-view UI (Live/Events/Stats/Inspector) |
| `run_experiments.sh` | `run_experiments.sh` | 4×4×10 = 160 experiment matrix |
| `readme.md` | `readme.md` | Dürüst yeniden yazım |
| — | `feature_engineering.py` | **YENİ** — Rolling window + aggressor scoring |
| — | `mitigation_verification.py` | **YENİ** — Before/after IPC karşılaştırma |

---

## Sunucuya Yüklerken Yapılacaklar

```bash
# 1. Eski klasörü yedekle
mv ~/cloud ~/cloud-backup-$(date +%Y%m%d)

# 2. Yeni klasörü kur
mkdir -p ~/cloud
cd ~/cloud
# Yeni dosyaları buraya kopyala (scp veya rsync ile)

# 3. Veri dosyalarını taşı (master_dataset.csv ÇIKAR — yeniden üreteceğiz)
rm -f master_dataset.csv rf_model.pkl baseline_ipc.pkl

# 4. STREAM binary'yi taşı
mkdir -p STREAM
cp ~/cloud-backup-*/STREAM/stream_c.exe STREAM/

# 5. Python venv'i yeniden kur
python3 -m venv telemetry_env
source telemetry_env/bin/activate
pip install flask flask-socketio "python-socketio[client]" scikit-learn pandas numpy joblib

# 6. Eski veri dosyalarını yeni schema ile uyumsuz, silinmeli
rm -f data_*.csv

# 7. Pipeline'ı baştan çalıştır
chmod +x run_experiments.sh
sudo bash run_experiments.sh           # ~3 saat sürer
python3 train_model.py                  # F1 0.85-0.95 olmalı
python3 app.py &                         # dashboard
sudo python3 live_daemon.py             # daemon
```

---

## Kritik Test Senaryoları

Yeni sistemi doğrulamak için şu testleri yap:

### Test 1: Eğitim verisi class balance kontrolü
```bash
python3 train_model.py
# Beklenen: Normal %60-70, Interference %30-40
# F1 0.85-0.95 arası
```

Eğer F1 = 1.00 görüyorsan, hâlâ label leakage var demektir.
Eğer F1 < 0.70 ise model yeterli öğrenmemiş.

### Test 2: Hysteresis çalışıyor mu?
Daemon başlatılınca 3 ardışık alarm gelmeden mitigation tetiklenmemeli.
Console output'ta `int=X/3 rec=Y/10` görmen lazım.

### Test 3: Dynamic aggressor detection
İki farklı core'a iki workload başlat, daemon'un doğru aggressor'ı
seçtiğini kontrol et:
- Core 0: STREAM (aggressor)
- Core 1: redis-benchmark (victim)
Beklenen: daemon "aggressor: core 0" demeli.

### Test 4: Mitigation verification
Mitigation tetiklendikten sonra `[verifier] Recovery: +X.X%` mesajı
gelmeli. Status 'effective' veya 'partial' olmalı.

---

## Phase 2 İçin Yol Haritası

1. Eğitim verisinin gerçek class balance'ını öğren (train_model çıktısı).
2. SPEC CPU 2017 ve PARSEC workload'larını kur.
3. Generalization test scripti yaz (eğitim setinde olmayan workload'larda
   modeli test et).
4. Effectiveness ölçüm scripti yaz (6 zorlu pair için before/after IPC
   karşılaştırması).
5. Overhead ölçümü: daemon çalışırken ve çalışmıyorken CPU kullanımı.
6. Paper draft (6 sayfa IEEE/ACM format).
