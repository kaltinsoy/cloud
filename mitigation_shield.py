"""
Mitigation Shield: Intel RDT CAT/MBA Controller (v3 — ASSOCIATION FIX)
======================================================================

Bu modül pqos aracılığıyla cache partitioning (CAT) ve memory bandwidth
kısıtlama (MBA) uygular. Dinamik attacker core seçimi, doğrulama ve
geri alma desteği vardır.

=====================================================================
KRİTİK HATA (v2'de keşfedildi — bu yüzden mitigation HİÇ çalışmıyordu):
=====================================================================
  Intel RDT'de cache partitioning İKİ ADIMLIDIR:
    1. COS (Class of Service) TANIMLA:  pqos -e "llc:1=0x003"
       → "1 numaralı sınıf sadece 2 way kullanır"
    2. Core'u o COS'a ATA:              pqos -a "llc:1=<core>"
       → "<core>, artık 1 numaralı sınıfa bağlı"

  v2 kodu SADECE 1. adımı yapıyordu (-e), 2. adımı (-a) HİÇ yapmıyordu.
  Sonuç: COS tanımlanıyor ama hiçbir core ona bağlanmıyordu.
  Attacker core default COS0'da (tüm cache + tüm bandwidth) kalıyordu.
  → Mitigation tam anlamıyla bir NO-OP idi. recovery hep ~%0 çıkıyordu.

  Ayrıca v2'de "llc:{core_id}=..." yazıyordu; buradaki sayı pqos için
  CORE değil COS ID'sidir. Core ile COS numarasının aynı olması (1)
  sadece tesadüftü ve yanıltıcıydı.

=====================================================================
YENİ TASARIM (v3):
=====================================================================
  - COS id'leri core'dan AYRILDI (sabit: attacker→COS1, victim→COS2)
  - Her apply_policy artık -e (tanım) + -a (atama) İKİSİNİ de yapar
  - reset, core'u tekrar COS0'a (default, full access) ATAR
  - State: hangi core hangi COS'a bağlı (reset için)
  - verify_association(): pqos -s parse ederek atamanın gerçekten
    olduğunu doğrular (paper'da "binding confirmed" demek için)

  KONTROL: her mitigation sonrası `sudo pqos -I -s` çalıştırıp
  core→COS tablosunda attacker core'un COS1'de olduğunu doğrula.
"""

import subprocess
import argparse
import sys
import re

# ============================================================
# CAT/MBA Politikaları
# ============================================================
# Cache way bitmasks (Xeon Gold 6136: 11-way L3, 0x7FF = full)
POLICIES = {
    'baseline': {
        'llc_mask': '0x7FF',  # Tüm 11 way (24.75 MB) — sınırsız
        'mba_pct':  100,      # %100 bandwidth
        'description': 'Full access (no restrictions)',
    },
    'isolate': {
        'llc_mask': '0x003',  # Sadece alt 2 way (~4.5 MB)
        'mba_pct':  100,      # Bandwidth dokunulmaz (SADECE cache izolasyonu)
        'description': 'Cache isolation (attacker pinned to 2 ways)',
    },
    'strangulate': {
        'llc_mask': '0x003',  # 2 way cache
        'mba_pct':  20,       # %20 bandwidth — bandwidth saldırıları için ŞART
        'description': 'Maximum throttling (cache + bandwidth)',
    },
    'mild': {
        'llc_mask': '0x00F',  # 4 way cache (~9 MB)
        'mba_pct':  50,       # %50 bandwidth
        'description': 'Mild restriction',
    },
}

# Victim korumalı sınıfının cache mask'i (attacker'ın kullanmadığı üst way'ler)
VICTIM_PROTECTED_MASK = '0x7FC'  # Way 2-10 (~22 MB), attacker'ın 0x003'ü ile çakışmaz

# ============================================================
# Sabit COS id'leri (core numarasından BAĞIMSIZ!)
# ============================================================
ATTACKER_COS = 1   # kısıtlı sınıf
VICTIM_COS   = 2   # korumalı sınıf (üst way'ler, tam bandwidth)
DEFAULT_COS  = 0   # pqos/resctrl default: tüm core'lar burada, full access

# State: {core_id: cos_id} — hangi core hangi COS'a bağlandı (reset için)
_active = {}


# ============================================================
# pqos Wrapper
# ============================================================
def _run_pqos(args, quiet=True):
    """pqos -I <args> çalıştır. Başarılıysa True döner."""
    cmd = ["sudo", "pqos", "-I"] + args
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            err = result.stderr.decode('utf-8', errors='replace').strip()
            print(f"[!] pqos başarısız: {' '.join(cmd)}")
            if err:
                print(f"    stderr: {err}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"[!] pqos timeout: {' '.join(cmd)}")
        return False
    except FileNotFoundError:
        print("[!] pqos bulunamadı. 'sudo apt install intel-cmt-cat' deneyin.")
        return False


def apply_policy(core_id, policy_name, victim_core=None):
    """
    Attacker core'a kısıtlama politikası uygula (TANIM + ATAMA).

    Args:
        core_id:     kısıtlanacak (attacker) core
        policy_name: 'baseline' | 'isolate' | 'strangulate' | 'mild'
        victim_core: korunacak victim core (üst way'lere atanır)

    Returns:
        True if başarılı (hem -e hem -a komutları geçtiyse)
    """
    if policy_name not in POLICIES:
        print(f"[-] Bilinmeyen policy: {policy_name}")
        return False

    if policy_name == 'baseline':
        return reset_core(core_id)

    policy = POLICIES[policy_name]
    print(f"[*] Core {core_id} → policy '{policy_name}'  (COS{ATTACKER_COS})")
    print(f"    {policy['description']}")
    print(f"    LLC mask: {policy['llc_mask']}, MBA: {policy['mba_pct']}%")

    # --- 1. ATTACKER sınıfını TANIMLA (-e) ---
    ok_def_llc = _run_pqos(["-e", f"llc:{ATTACKER_COS}={policy['llc_mask']}"])
    ok_def_mba = _run_pqos(["-e", f"mba:{ATTACKER_COS}={policy['mba_pct']}"])

    # --- 2. *** ATTACKER CORE'U O SINIFA BAĞLA (-a) — v2'DE EKSİK OLAN ADIM *** ---
    ok_assoc = _run_pqos(["-a", f"llc:{ATTACKER_COS}={core_id}"])

    # --- 3. VICTIM'i koru: ayrı sınıf, üst way'ler, tam bandwidth ---
    if victim_core is not None:
        _run_pqos(["-e", f"llc:{VICTIM_COS}={VICTIM_PROTECTED_MASK}"])
        _run_pqos(["-e", f"mba:{VICTIM_COS}=100"])
        ok_v = _run_pqos(["-a", f"llc:{VICTIM_COS}={victim_core}"])
        if ok_v:
            _active[victim_core] = VICTIM_COS
        else:
            print("[!] Victim COS ataması yapılamadı, devam ediliyor.")

    if ok_def_llc and ok_def_mba and ok_assoc:
        _active[core_id] = ATTACKER_COS
        print(f"[✓] Core {core_id} → COS{ATTACKER_COS} bağlandı (tanım+atama OK).")
        return True
    else:
        print("[!] Policy tam uygulanamadı, baseline'a dönülüyor.")
        reset_core(core_id)
        return False


def apply_policy_cores(attacker_cores, policy_name, victim_core=None):
    """
    Birden çok attacker core'una AYNI kısıtlama policy'sini uygula.
    (Çok-core bandwidth saldırısı senaryosu: cores 1-5 hepsi tek COS'a bağlanır.)
    """
    if policy_name not in POLICIES or policy_name == 'baseline':
        print(f"[-] Geçersiz policy (multi): {policy_name}")
        return False

    policy = POLICIES[policy_name]
    core_list = ",".join(str(c) for c in attacker_cores)
    print(f"[*] Cores [{core_list}] → '{policy_name}' (COS{ATTACKER_COS})")
    print(f"    LLC mask: {policy['llc_mask']}, MBA: {policy['mba_pct']}%")

    # 1. Attacker sınıfını tanımla (-e)
    ok_def_llc = _run_pqos(["-e", f"llc:{ATTACKER_COS}={policy['llc_mask']}"])
    ok_def_mba = _run_pqos(["-e", f"mba:{ATTACKER_COS}={policy['mba_pct']}"])
    # 2. TÜM attacker core'larını tek seferde bu sınıfa bağla (-a)
    ok_assoc = _run_pqos(["-a", f"llc:{ATTACKER_COS}={core_list}"])
    # 3. Victim'i koru (ayrı sınıf, üst way'ler, tam bandwidth)
    if victim_core is not None:
        _run_pqos(["-e", f"llc:{VICTIM_COS}={VICTIM_PROTECTED_MASK}"])
        _run_pqos(["-e", f"mba:{VICTIM_COS}=100"])
        if _run_pqos(["-a", f"llc:{VICTIM_COS}={victim_core}"]):
            _active[victim_core] = VICTIM_COS

    if ok_def_llc and ok_def_mba and ok_assoc:
        for c in attacker_cores:
            _active[c] = ATTACKER_COS
        print(f"[✓] {len(attacker_cores)} core → COS{ATTACKER_COS} bağlandı.")
        return True
    else:
        print("[!] Multi-core policy uygulanamadı, baseline'a dönülüyor.")
        reset_cores(attacker_cores)
        return False


def reset_cores(cores):
    """Bir liste core'u default COS0'a (full access) geri ata."""
    if not cores:
        return True
    core_list = ",".join(str(c) for c in cores)
    print(f"[*] Cores [{core_list}] → COS{DEFAULT_COS} (baseline)")
    ok = _run_pqos(["-a", f"llc:{DEFAULT_COS}={core_list}"])
    if ok:
        for c in cores:
            _active.pop(c, None)
    return ok


def reset_core(core_id):
    """Core'u tekrar default COS0'a (full access) ATA."""
    print(f"[*] Core {core_id} → COS{DEFAULT_COS} (baseline) atanıyor")
    ok = _run_pqos(["-a", f"llc:{DEFAULT_COS}={core_id}"])
    if ok:
        _active.pop(core_id, None)
        print(f"[✓] Core {core_id} baseline'a (COS0) döndü.")
    return ok


def reset_all():
    """Tüm bağlı core'ları COS0'a geri ata."""
    for c in list(_active.keys()):
        reset_core(c)


def get_active_policies():
    """Şu an hangi core hangi COS'a bağlı."""
    return dict(_active)


# ============================================================
# Atama Doğrulama (paper'da "binding confirmed" demek için)
# ============================================================
def verify_association(core_id, expected_cos):
    """
    `pqos -I -s` çıktısını parse edip core_id'nin gerçekten
    expected_cos'a bağlı olduğunu doğrular.

    Returns:
        True  → core gerçekten beklenen COS'a bağlı
        False → değil (mitigation uygulanmamış!)
        None  → parse edilemedi
    """
    try:
        result = subprocess.run(
            ["sudo", "pqos", "-I", "-s"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=10,
        )
    except Exception as e:
        print(f"[!] pqos -s çalıştırılamadı: {e}")
        return None

    # pqos -s çıktısında "Core <id>, ... => COS<n>" benzeri satırlar olur.
    # Versiyona göre format değişebildiği için esnek regex.
    for line in result.stdout.splitlines():
        if re.search(rf'\bCore\s+{core_id}\b', line):
            m = re.search(r'COS\s*(\d+)', line)
            if m:
                actual = int(m.group(1))
                ok = (actual == expected_cos)
                sym = '✓' if ok else '✗'
                print(f"[{sym}] Core {core_id} → COS{actual} "
                      f"(beklenen COS{expected_cos})")
                return ok
    print(f"[?] Core {core_id} atama satırı pqos -s çıktısında bulunamadı.")
    return None


def show_state():
    """Mevcut CAT/MBA durumunu ekrana bas (manuel kontrol için)."""
    subprocess.run(["sudo", "pqos", "-I", "-s"], check=False)


# ============================================================
# CLI (geri uyumluluk)
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='Active Defense: Intel CAT/MBA Controller (v3)'
    )
    parser.add_argument('--attacker_core', type=int, required=True,
                        help='Kısıtlanacak core ID')
    parser.add_argument('--action', choices=list(POLICIES.keys()), required=True,
                        help='Mitigation policy')
    parser.add_argument('--victim_core', type=int, default=None,
                        help='Korunacak victim core')
    parser.add_argument('--show', action='store_true',
                        help='Uyguladıktan sonra pqos -s göster')
    parser.add_argument('--verify', action='store_true',
                        help='Atamanın gerçekten olduğunu doğrula')
    args = parser.parse_args()

    if args.action == 'baseline':
        ok = reset_core(args.attacker_core)
    else:
        ok = apply_policy(args.attacker_core, args.action,
                          victim_core=args.victim_core)

    if args.verify and args.action != 'baseline':
        verify_association(args.attacker_core, ATTACKER_COS)

    if args.show:
        print("\n[*] Mevcut CAT/MBA durumu:")
        show_state()

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
