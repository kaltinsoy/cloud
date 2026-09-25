"""
Mitigation Verification Module
================================

Mitigation öncesi ve sonrası IPC ölçerek müdahalenin etkili olup
olmadığını doğrular. Recovery percentage hesaplar.

NEDEN GEREKLİ:
  - Mitigation körü körüne uygulanmamalı
  - "İşe yaradı mı?" sorusuna sayısal cevap lazım
  - Paper'da effectiveness metric'i raporlamak için kritik
  - Demo'da jüriye "IPC %X geri kazanıldı" diyebilmek için

KULLANIM:
    from mitigation_verification import MitigationVerifier

    verifier = MitigationVerifier(observer=observe_function)
    verifier.snapshot_before()
    apply_mitigation()
    time.sleep(3)  # stabilize
    result = verifier.snapshot_after_and_compare()
    print(f"IPC recovery: {result['recovery_percent']:.1f}%")
"""

import time
import subprocess
from collections import deque


# Recovery threshold (paper'da bu değer raporlanır)
RECOVERY_GOOD_THRESHOLD = 30.0    # %30+ = effective
RECOVERY_PARTIAL_THRESHOLD = 10.0  # %10-30 = partial
# %10 altı = ineffective


class MitigationVerifier:
    """
    Mitigation öncesi/sonrası performans karşılaştırması.

    observer: çağrıldığında {'IPC': float, ...} dict döndüren callable
    """

    def __init__(self, observer, samples_per_phase=7, sample_interval=1.0):
        """
        Args:
            observer: () -> dict, sistem ölçümü yapan fonksiyon
            samples_per_phase: kaç sample alınacak (before ve after için)
            sample_interval: sample'lar arası saniye
        """
        self.observer = observer
        self.samples_per_phase = samples_per_phase
        self.sample_interval = sample_interval

        self.before_samples = []
        self.after_samples = []

    def _collect_samples(self, n):
        """n adet sample topla."""
        samples = []
        for _ in range(n):
            try:
                sample = self.observer()
                if sample and 'IPC' in sample:
                    samples.append(sample)
            except Exception as e:
                print(f"[!] Sample alınamadı: {e}")
            time.sleep(self.sample_interval)
        return samples

    def snapshot_before(self):
        """Mitigation öncesi baseline al."""
        print(f"[verifier] Before snapshot alınıyor "
              f"({self.samples_per_phase} sample)...")
        self.before_samples = self._collect_samples(self.samples_per_phase)
        if not self.before_samples:
            print("[verifier] UYARI: Before sample alınamadı!")
            return False
        avg_ipc = self._avg_ipc(self.before_samples)
        print(f"[verifier] Before IPC ortalaması: {avg_ipc:.4f}")
        return True

    def snapshot_after_and_compare(self):
        """
        Mitigation sonrası ölçüm yap ve before ile karşılaştır.

        Returns:
            dict: {
                'before_ipc': float,
                'after_ipc': float,
                'ipc_change': float,           # mutlak fark
                'recovery_percent': float,     # % değişim
                'status': str,                 # 'effective', 'partial', 'ineffective'
                'before_mb_per_sec': float,
                'after_mb_per_sec': float,
            }
        """
        print(f"[verifier] After snapshot alınıyor...")
        self.after_samples = self._collect_samples(self.samples_per_phase)

        if not self.before_samples:
            return self._empty_result("Before sample yok")
        if not self.after_samples:
            return self._empty_result("After sample alınamadı")

        before_ipc = self._avg_ipc(self.before_samples)
        after_ipc = self._avg_ipc(self.after_samples)

        if before_ipc <= 0:
            return self._empty_result("Before IPC sıfır veya negatif")

        ipc_change = after_ipc - before_ipc
        recovery_percent = (ipc_change / before_ipc) * 100

        # Status sınıflandırması
        if recovery_percent >= RECOVERY_GOOD_THRESHOLD:
            status = 'effective'
        elif recovery_percent >= RECOVERY_PARTIAL_THRESHOLD:
            status = 'partial'
        else:
            status = 'ineffective'

        before_mb = self._avg_field(self.before_samples, 'Total_MB_per_sec')
        after_mb = self._avg_field(self.after_samples, 'Total_MB_per_sec')

        result = {
            'before_ipc': before_ipc,
            'after_ipc': after_ipc,
            'ipc_change': ipc_change,
            'recovery_percent': recovery_percent,
            'status': status,
            'before_mb_per_sec': before_mb,
            'after_mb_per_sec': after_mb,
        }

        # Konsola özet bas
        symbol = '✓' if status == 'effective' else ('~' if status == 'partial' else '✗')
        print(f"[verifier] [{symbol}] Sonuç: {status.upper()}")
        print(f"    Before IPC: {before_ipc:.4f}")
        print(f"    After IPC:  {after_ipc:.4f}")
        print(f"    Recovery:   {recovery_percent:+.1f}%")
        print(f"    MB/s: {before_mb:.0f} → {after_mb:.0f}")

        return result

    def reset(self):
        """State'i temizle."""
        self.before_samples = []
        self.after_samples = []

    # --- Yardımcılar ---
    @staticmethod
    def _avg_ipc(samples):
        ipcs = [s.get('IPC', 0) for s in samples if 'IPC' in s]
        return sum(ipcs) / len(ipcs) if ipcs else 0.0

    @staticmethod
    def _avg_field(samples, field):
        values = [s.get(field, 0) for s in samples if field in s]
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _empty_result(reason):
        return {
            'before_ipc': 0.0,
            'after_ipc': 0.0,
            'ipc_change': 0.0,
            'recovery_percent': 0.0,
            'status': 'unknown',
            'before_mb_per_sec': 0.0,
            'after_mb_per_sec': 0.0,
            'reason': reason,
        }


# ============================================================
# Standalone test
# ============================================================
def _mock_observer():
    """Test için sahte observer (gerçek sistemde live_daemon kullanır)."""
    import random
    return {
        'IPC': 1.5 + random.uniform(-0.1, 0.1),
        'Total_MB_per_sec': 5000 + random.randint(-500, 500),
    }


if __name__ == "__main__":
    print("[*] Mitigation Verifier smoke test")
    verifier = MitigationVerifier(
        observer=_mock_observer,
        samples_per_phase=3,
        sample_interval=0.5,
    )
    verifier.snapshot_before()
    print("[*] Pretending mitigation is applied...")
    time.sleep(1)
    result = verifier.snapshot_after_and_compare()
    print(f"\nFull result: {result}")
