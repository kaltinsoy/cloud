"""
Noisy-Neighbor Telemetry Collector (v2 — deadlock fix)
========================================================

ESKI HATA (v1): subprocess.Popen(stderr=subprocess.PIPE) kullanıyordu,
perf 60 saniyede çok satır yazınca PIPE buffer doluyordu, perf bloke
oluyordu, wait() asla dönmüyordu → DEADLOCK.

Belirti: perf process'leri "T" (stopped) state'de takılı kalıyor,
CSV dosyası oluşturulmuyor, script asla tamamlanmıyor.

ÇÖZÜM (v2):
  1. stderr → file ya da DEVNULL (PIPE deadlock'u önle)
  2. stdin = DEVNULL (tty etkileşimi yok)
  3. start_new_session=True (parent tty'den ayır)
  4. Timeout ekle (her ihtimale karşı)

Bu telemetry.py her 100ms'de bir core 0 (victim) ve core 1 (attacker)
için perf counter ölçümü yapar. Sistem geneli için uncore IMC.
"""

import subprocess
import sys
import os
import argparse
import pandas as pd
import numpy as np


# ============================================================
# AYARLAR
# ============================================================
INTERVAL_MS = 100
DURATION_SEC = 60
VICTIM_CORE = 0
ATTACKER_CORE = 1

CORE_EVENTS = "instructions,cycles,LLC-load-misses,LLC-loads"

UNCORE_EVENTS = (
    "uncore_imc_0/cas_count_read/,uncore_imc_0/cas_count_write/,"
    "uncore_imc_1/cas_count_read/,uncore_imc_1/cas_count_write/,"
    "uncore_imc_2/cas_count_read/,uncore_imc_2/cas_count_write/,"
    "uncore_imc_3/cas_count_read/,uncore_imc_3/cas_count_write/,"
    "uncore_imc_4/cas_count_read/,uncore_imc_4/cas_count_write/,"
    "uncore_imc_5/cas_count_read/,uncore_imc_5/cas_count_write/"
)


# ============================================================
# PERF CSV PARSER
# ============================================================
def parse_perf_csv(filepath):
    """perf stat -x ',' formatındaki CSV'yi parse et."""
    records = []
    if not os.path.exists(filepath):
        return records

    with open(filepath, 'r') as f:
        for line in f:
            if line.startswith('#') or not line.strip():
                continue

            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 4:
                continue
            if parts[1] in ['<not counted>', '<not supported>', '']:
                continue

            try:
                val = float(parts[1])
                unit = parts[2] if len(parts) > 2 else ''
                event_name = parts[3] if len(parts) > 3 else ''

                # perf'in otomatik scaling'ini ham CAS count'a çevir
                # (CAS = 64 byte per transfer)
                if 'GiB' in unit:
                    val = (val * 1073741824) / 64
                elif 'MiB' in unit:
                    val = (val * 1048576) / 64
                elif 'KiB' in unit:
                    val = (val * 1024) / 64
                elif unit in ('B', 'Bytes'):
                    val = val / 64

                records.append({
                    "timestamp": float(parts[0]),
                    "event": event_name,
                    "value": val,
                })
            except (ValueError, IndexError):
                continue

    return records


# ============================================================
# PERF KOMUTU OLUŞTURUCU
# ============================================================
def make_perf_cmd(events, core_or_all, output_file):
    """perf stat komutu oluştur."""
    cmd = [
        "perf", "stat",
        "-I", str(INTERVAL_MS),
        "-x", ",",
        "-e", events,
        "-o", output_file,
    ]
    if core_or_all == "all":
        cmd.append("-a")
    else:
        cmd.extend(["-C", str(core_or_all)])

    cmd.extend(["sleep", str(DURATION_SEC)])
    return cmd


# ============================================================
# DEADLOCK-SAFE SUBPROCESS LAUNCHER
# ============================================================
def launch_perf(cmd, stderr_log):
    """
    Perf process'i başlat — DEADLOCK-SAFE şekilde.

    Önemli noktalar:
      - stdin=DEVNULL: tty etkileşimi yok
      - stderr=stderr_log (dosya): PIPE buffer deadlock yok
      - stdout=DEVNULL: perf zaten -o file'a yazıyor
      - start_new_session=True: parent tty'den ayır
    """
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr_log,
        start_new_session=True,
    )


# ============================================================
# FEATURE ENGINEERING (PER-CORE)
# ============================================================
def engineer_core_features(df_core, prefix):
    """Tek core için IPC, miss rate, cache dominance."""
    df = df_core.copy()

    if 'instructions' in df.columns and 'cycles' in df.columns:
        df[f'{prefix}IPC'] = df['instructions'] / df['cycles'].replace(0, np.nan)

    if 'LLC-loads' in df.columns and 'LLC-load-misses' in df.columns:
        df[f'{prefix}miss_rate'] = (
            df['LLC-load-misses'] / df['LLC-loads'].replace(0, np.nan)
        )

    if all(c in df.columns for c in ['LLC-loads', 'LLC-load-misses', 'cycles']):
        llc_intensity = df['LLC-loads'] / df['cycles'].replace(0, np.nan)
        miss_rate = (df['LLC-load-misses']
                     / df['LLC-loads'].replace(0, np.nan)).fillna(0)
        df[f'{prefix}cache_dominance'] = llc_intensity * (1 - miss_rate)

    df = df.rename(columns={
        'instructions': f'{prefix}instructions',
        'cycles':       f'{prefix}cycles',
        'LLC-loads':    f'{prefix}LLC-loads',
        'LLC-load-misses': f'{prefix}LLC-load-misses',
    })

    return df


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Co-located workload telemetry (her iki core)."
    )
    parser.add_argument('--victim', required=True)
    parser.add_argument('--attacker', required=True)
    parser.add_argument('--run_id', required=True)
    parser.add_argument('--duration', type=int, default=DURATION_SEC)
    args = parser.parse_args()

    print(f"[*] Telemetry: {args.victim} (core {VICTIM_CORE}) "
          f"vs {args.attacker} (core {ATTACKER_CORE}), {args.duration}s")

    # Geçici dosyalar
    f_victim_out = "tmp_victim_out.csv"
    f_attacker_out = "tmp_attacker_out.csv"
    f_uncore_out = "tmp_uncore_out.csv"
    f_victim_err = "tmp_victim_err.log"
    f_attacker_err = "tmp_attacker_err.log"
    f_uncore_err = "tmp_uncore_err.log"

    # Eski geçici dosyaları temizle
    for f in (f_victim_out, f_attacker_out, f_uncore_out,
              f_victim_err, f_attacker_err, f_uncore_err):
        if os.path.exists(f):
            os.remove(f)

    # 3 perf instance'ı paralel başlat (deadlock-safe)
    cmd_victim = make_perf_cmd(CORE_EVENTS, VICTIM_CORE, f_victim_out)
    cmd_attacker = make_perf_cmd(CORE_EVENTS, ATTACKER_CORE, f_attacker_out)
    cmd_uncore = make_perf_cmd(UNCORE_EVENTS, "all", f_uncore_out)

    # stderr'i dosyaya yönlendir — PIPE deadlock'u önle
    log_v = open(f_victim_err, 'w')
    log_a = open(f_attacker_err, 'w')
    log_u = open(f_uncore_err, 'w')

    timeout_sec = args.duration + 30  # safety margin

    try:
        p_v = launch_perf(cmd_victim, log_v)
        p_a = launch_perf(cmd_attacker, log_a)
        p_u = launch_perf(cmd_uncore, log_u)

        # Timeout-bounded wait
        try:
            p_v.wait(timeout=timeout_sec)
            p_a.wait(timeout=timeout_sec)
            p_u.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            print(f"[!] Perf timeout ({timeout_sec}s sonra hâlâ koşuyor) — sonlandırılıyor")
            for p in (p_v, p_a, p_u):
                try:
                    p.kill()
                    p.wait(timeout=5)
                except Exception:
                    pass
            sys.exit(2)

    except KeyboardInterrupt:
        print("\n[*] Interrupt — perf process'leri durduruluyor...")
        for p in (p_v, p_a, p_u):
            try:
                p.terminate()
            except Exception:
                pass
        sys.exit(1)
    finally:
        log_v.close()
        log_a.close()
        log_u.close()

    print("[*] Perf ölçümleri tamamlandı, veri işleniyor...")

    # Hata kontrolü — stderr log'larına bak
    def check_stderr(filepath, name):
        if os.path.exists(filepath):
            with open(filepath) as f:
                content = f.read().strip()
            if content and 'failed' in content.lower():
                print(f"[!] {name} stderr:")
                print(content[:500])

    check_stderr(f_victim_err, "Victim core")
    check_stderr(f_attacker_err, "Attacker core")
    check_stderr(f_uncore_err, "Uncore IMC")

    # Parse
    victim_data = parse_perf_csv(f_victim_out)
    attacker_data = parse_perf_csv(f_attacker_out)
    uncore_data = parse_perf_csv(f_uncore_out)

    if not victim_data:
        print("[-] HATA: Victim core verisi yok!")
        sys.exit(1)
    if not attacker_data:
        print("[-] HATA: Attacker core verisi yok!")
        sys.exit(1)
    if not uncore_data:
        print("[-] HATA: Uncore IMC verisi yok!")
        sys.exit(1)

    # DataFrame'lere çevir
    df_v = pd.DataFrame(victim_data)
    df_a = pd.DataFrame(attacker_data)
    df_u = pd.DataFrame(uncore_data)

    # Timestamp'leri 0.1s hassasiyetine yuvarla
    for df in (df_v, df_a, df_u):
        df['time_step'] = df['timestamp'].round(1)

    # Pivot
    df_v_pivot = df_v.pivot_table(
        index='time_step', columns='event', values='value'
    ).reset_index()
    df_a_pivot = df_a.pivot_table(
        index='time_step', columns='event', values='value'
    ).reset_index()

    # Uncore: tüm 6 kanalı topla (sistem geneli)
    df_u_total = df_u.groupby('time_step')['value'].sum().reset_index()
    df_u_total.rename(columns={'value': 'cas_count_total'}, inplace=True)

    # Per-core feature engineering
    df_v_pivot = engineer_core_features(df_v_pivot, prefix='victim_')
    df_a_pivot = engineer_core_features(df_a_pivot, prefix='attacker_')

    # Üçünü timestamp'te merge
    df_master = df_v_pivot.merge(df_a_pivot, on='time_step', how='inner')
    df_master = df_master.merge(df_u_total, on='time_step', how='inner')

    # Sistem geneli memory bandwidth (MB/s)
    df_master['Total_MB_per_sec'] = (
        df_master['cas_count_total'] * 64 / 1048576
    ) * 10

    # Geri uyumluluk için (train_model.py'nin beklediği isimler)
    if 'victim_IPC' in df_master.columns:
        df_master['IPC'] = df_master['victim_IPC']
    if 'victim_cache_dominance' in df_master.columns:
        df_master['cache_dominance_score'] = df_master['victim_cache_dominance']
    if 'victim_LLC-load-misses' in df_master.columns:
        df_master['LLC-load-misses'] = df_master['victim_LLC-load-misses']
    if 'victim_LLC-loads' in df_master.columns:
        df_master['LLC-loads'] = df_master['victim_LLC-loads']
    if 'victim_instructions' in df_master.columns:
        df_master['instructions'] = df_master['victim_instructions']
    if 'victim_cycles' in df_master.columns:
        df_master['cycles'] = df_master['victim_cycles']

    df_master['victim'] = args.victim
    df_master['attacker'] = args.attacker
    df_master['run_id'] = args.run_id

    front_cols = [
        'time_step', 'victim', 'attacker', 'run_id',
        'IPC', 'Total_MB_per_sec', 'cache_dominance_score',
        'LLC-load-misses', 'LLC-loads', 'instructions', 'cycles',
        'cas_count_total',
        'victim_IPC', 'victim_miss_rate', 'victim_cache_dominance',
        'attacker_IPC', 'attacker_miss_rate', 'attacker_cache_dominance',
    ]
    front_cols = [c for c in front_cols if c in df_master.columns]
    other_cols = [c for c in df_master.columns if c not in front_cols]
    df_master = df_master[front_cols + other_cols]

    df_master = df_master.replace([np.inf, -np.inf], np.nan).dropna(subset=['IPC'])

    output_file = f"data_{args.victim}_vs_{args.attacker}_run{args.run_id}.csv"
    df_master.to_csv(output_file, index=False)

    # Temp dosyaları sil
    for f in (f_victim_out, f_attacker_out, f_uncore_out,
              f_victim_err, f_attacker_err, f_uncore_err):
        if os.path.exists(f):
            os.remove(f)

    print(f"[+] {len(df_master)} sample → {output_file}")
    if len(df_master) > 0:
        print("\nÖrnek satırlar:")
        cols_show = ['time_step', 'IPC', 'Total_MB_per_sec',
                     'attacker_IPC', 'attacker_cache_dominance']
        cols_show = [c for c in cols_show if c in df_master.columns]
        print(df_master[cols_show].head().to_string())


if __name__ == "__main__":
    main()
