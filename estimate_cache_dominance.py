def estimate_cache_dominance(metrics):
    """
    Bir workload'un L3 cache üzerindeki baskınlığını tahmin et.
    Yüksek LLC-loads + düşük miss rate = cache'te baskın
    Yüksek LLC-loads + yüksek miss rate = cache'te eziliyor (kurban)
    """
    llc_intensity = metrics['LLC_loads'] / metrics['cycles']
    miss_rate = metrics['LLC_misses'] / metrics['LLC_loads']
    
    dominance_score = llc_intensity * (1 - miss_rate)
    return dominance_score
