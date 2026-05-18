"""
Mitigation Shield: Intel RDT CAT/MBA Controller
================================================

Bu modül pqos aracılığıyla cache partitioning (CAT) ve memory bandwidth
kısıtlama (MBA) uygular. Dinamik attacker core seçimi, doğrulama ve
geri alma desteği vardır.

ÖNCEKİ HATA:
  - attacker_core hardcoded olarak "1" idi
  - Sadece komut satırından kullanılabiliyordu
  - Python modülü olarak import edilemiyordu (live_daemon'dan)

YENİ TASARIM:
  - Hem CLI hem Python API
  - Dynamic core seçimi
  - 3 policy: baseline, isolate, strangulate
  - Hata durumunda otomatik baseline'a dön
  - State tracking (hangi core hangi politika altında)
"""

import subprocess
import argparse
import sys
import os

# ============================================================
# CAT/MBA Politikaları
# ============================================================
# Cache way bitmasks (Xeon Gold 6136: 11-way L3, 0x7FF = full)
POLICIES = {
    'baseline': {
        'llc_mask': '0x7FF',  # Tüm 11 way (24.75 MB) — sınırsız erişim
        'mba_pct':  100,      # %100 bandwidth
        'description': 'Full access (no restrictions)',
    },
    'isolate': {
        'llc_mask': '0x003',  # Sadece alt 2 way (~4.5 MB)
        'mba_pct':  100,      # Bandwidth dokunulmaz
        'description': 'Cache isolation (attacker pinned to 2 ways)',
    },
    'strangulate': {
        'llc_mask': '0x003',  # 2 way cache
        'mba_pct':  20,       # %20 bandwidth (sıkı kısıtlama)
        'description': 'Maximum throttling (cache + bandwidth)',
    },
    'mild': {
        'llc_mask': '0x00F',  # 4 way cache (~9 MB)
        'mba_pct':  50,       # %50 bandwidth
        'description': 'Mild restriction',
    },
}

# Victim için yedek mask (attacker dışı alanlar)
# Eğer attacker isolate'lendiyse, victim üst way'leri kullansın
VICTIM_ISOLATED_MASK = '0x7FC'  # Way 2-10 (yaklaşık 22 MB), attacker'ın altındaki way'ler


# ============================================================
# State (process boyunca aktif politikalar)
# ============================================================
_active_policies = {}  # {core_id: policy_name}


# ============================================================
# pqos Wrapper
# ============================================================
def _run_pqos(args, quiet=True):
    """
    pqos komutunu çalıştır.

    Returns:
        True if başarılı, False if hatalı
    """
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
    Bir core'a politika uygula.

    Args:
        core_id: Hedef core id (genelde attacker)
        policy_name: 'baseline', 'isolate', 'strangulate', 'mild'
        victim_core: Eğer attacker isolate edilirse, victim'in üst way'leri
                     kullanabilmesi için (opsiyonel)

    Returns:
        True if başarılı
    """
    if policy_name not in POLICIES:
        print(f"[-] Bilinmeyen policy: {policy_name}")
        return False

    policy = POLICIES[policy_name]
    print(f"[*] Core {core_id} → policy '{policy_name}' uygulanıyor")
    print(f"    {policy['description']}")
    print(f"    LLC mask: {policy['llc_mask']}, MBA: {policy['mba_pct']}%")

    # 1. LLC mask uygula (CAT)
    ok_llc = _run_pqos(["-e", f"llc:{core_id}={policy['llc_mask']}"])

    # 2. MBA uygula
    ok_mba = _run_pqos(["-e", f"mba:{core_id}={policy['mba_pct']}"])

    # 3. (Opsiyonel) Victim'e üst way'leri ver
    if policy_name in ('isolate', 'strangulate') and victim_core is not None:
        ok_v = _run_pqos(["-e", f"llc:{victim_core}={VICTIM_ISOLATED_MASK}"])
        if not ok_v:
            print("[!] Victim cache mask uygulanamadı, devam ediliyor.")

    if ok_llc and ok_mba:
        _active_policies[core_id] = policy_name
        print(f"[✓] Policy '{policy_name}' core {core_id} üzerinde aktif.")
        return True
    else:
        # Kısmen başarısız: geri al
        print(f"[!] Policy uygulanamadı, baseline'a geri dönülüyor.")
        reset_core(core_id)
        return False


def reset_core(core_id):
    """Bir core'u baseline'a (kısıtsız) geri çevir."""
    print(f"[*] Core {core_id} baseline'a sıfırlanıyor")
    ok_llc = _run_pqos(["-e", f"llc:{core_id}={POLICIES['baseline']['llc_mask']}"])
    ok_mba = _run_pqos(["-e", f"mba:{core_id}={POLICIES['baseline']['mba_pct']}"])
    if ok_llc and ok_mba:
        _active_policies.pop(core_id, None)
        print(f"[✓] Core {core_id} baseline'a döndü.")
        return True
    return False


def reset_all():
    """Tüm aktif politikaları kaldır."""
    cores = list(_active_policies.keys())
    for c in cores:
        reset_core(c)


def get_active_policies():
    """Şu an aktif politikaları döndür."""
    return dict(_active_policies)


# ============================================================
# CLI (geri uyumluluk için)
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='Active Defense: Intel CAT/MBA Controller'
    )
    parser.add_argument(
        '--attacker_core',
        type=int,
        required=True,
        help='Mitigation uygulanacak core ID'
    )
    parser.add_argument(
        '--action',
        choices=list(POLICIES.keys()),
        required=True,
        help='Mitigation policy'
    )
    parser.add_argument(
        '--victim_core',
        type=int,
        default=None,
        help='Opsiyonel: victim core (isolate sırasında üst way verir)'
    )
    parser.add_argument(
        '--show',
        action='store_true',
        help='Uygulamadan sonra mevcut durumu göster'
    )
    args = parser.parse_args()

    if args.action == 'baseline':
        ok = reset_core(args.attacker_core)
    else:
        ok = apply_policy(
            args.attacker_core,
            args.action,
            victim_core=args.victim_core,
        )

    if args.show:
        print("\n[*] Mevcut CAT/MBA durumu:")
        subprocess.run(["sudo", "pqos", "-I", "-s"], check=False)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
