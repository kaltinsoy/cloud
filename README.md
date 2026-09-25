# Koray — sunucuda koşulacak 4 deney (hakem geri bildirimi için)

Paper'ı hakeme karşı sağlamlaştırmak için 4 ölçüm. **1 ve 2 şart**, 3 çok değerli (ilk sonuç figürü), 4 opsiyonel.
Hepsi tek NUMA node 0'da (victim core 0, aggressor core 1–5), mevcut `mitigation_shield.py` + `perf` ile çalışır — telemetry.py'deki ölçüm desenini birebir kullanıyor.

## Ön-koşullar
- Dosyaları `mitigation_shield.py`'nin olduğu dizine koy (server: muhtemelen `~/cloud2/`).
- Her scriptin başındaki **CONFIG** bloğunda `STREAM` (ve gerekiyorsa `MCF`) yolunu kendi binary'lerine göre düzelt.
  - `MCF=/usr/local/bin/mcf` (MemBench) — sende doğruysa dokunma.
  - `STREAM=...` — STREAM binary'nin tam yolu (örn. `~/cloud2/stream` ya da `/usr/local/bin/stream`).
- `perf` ve mitigation için **root** gerekiyor (`sudo`).
- Başlamadan önce temiz durum: `sudo python3 -c "import mitigation_shield as m; m.reset_all()"`

---

## DENEY 1 — Redis tail latency (p99)  ★ŞART
**Neden:** Paper Redis'i sadece IPC ile ölçüyor (zayıf proxy → "ineffective" görünüyor). p99 latency + throughput gerçek SLO metriği; ölü Redis satırlarını anlamlı sonuca çevirir.
```bash
bash exp1_redis_tail.sh
```
**Gönder:** `redis_A_solo.txt`, `redis_B_attacked.txt`, `redis_C_mitigated.txt`
(her dosyada "requests per second" + p99 içeren "Latency by percentile distribution" tablosu var)

## DENEY 2 — Aggressor taraması 0→6 core  ★ŞART
**Neden:** Headline "+81.6%" tek noktadan geliyor. 0–6 saldırgan için victim IPC + bellek bandwidth eğrisi "doygunluk dizini"ni (knee) gösterir → "single vs multi" (2 nokta) yerine "intensity'ye koşullu" iddiasını kanıtlar.
```bash
sudo python3 exp2_sweep.py
```
**Gönder:** `sweep.csv`  (kolonlar: n_aggressors, victim_ipc, mem_bw_GBps, llc_load_misses, llc_loads)

## DENEY 3 — IPC zaman serisi (sonuç figürü)  ◇çok değerli
**Neden:** Paper'da Tablo II'yi canlandıran hiçbir grafik yok. Bir tam döngüde (saldırı→mitigation→release) victim IPC'sinin çöküp toparlanması = en güçlü görsel.
```bash
sudo python3 exp3_timeseries.py
```
**Gönder:** `ts_raw.csv`  (faz offsetleri sabit: attack=8s, mitigate=20s, release=35s — script yazdırır)

## DENEY 4 — Blind-mitigation maliyeti  ○opsiyonel
**Neden:** Tezin temeli "gereksiz mitigation zarar verir" ama bu zarar hiç ölçülmemiş. Tek saldırganda (zarar yokken) mitigation uygulayıp aggressor throughput'unun düştüğünü, victim'in hiçbir şey kazanmadığını gösterir.
```bash
sudo python3 exp4_blind_cost.py | tee blind_cost.txt
```
**Gönder:** `blind_cost.txt` (ya da ekrandaki BEFORE/AFTER tablosu)

---

## Notlar
- Scriptler kendi temizliğini yapar (aggressor + victim öldürülür, mitigation reset edilir). Yine de bir deney yarıda kalırsa: `sudo python3 -c "import mitigation_shield as m; m.reset_all()"; pkill -f stream; pkill -f mcf`
- DENEY 2/3 victim'i (mcf) sürekli döngüde koşturur (`while true`), `perf -C 0` core'u ölçer — hangi process olduğu önemli değil.
- Bir şey event-name hatası verirse (`uncore_imc_*`): `perf list | grep cas_count` ile sendeki tam isme bakıp scriptteki `IMC_EVENTS`/`uncore_imc_{i}` desenini ona göre düzelt (telemetry.py ile aynı, çalışması lazım).
- Hepsini koşmak ~15–20 dk. Çıktı dosyalarını bana yolla, ben paper'a işleyip figürleri üretirim.
