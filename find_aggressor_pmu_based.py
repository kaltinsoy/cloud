def find_aggressor_pmu_based(workloads):
    scores = {}
    for wl_id, m in workloads.items():
        llc_intensity = m['LLC_loads'] / m['cycles']
        imc_intensity = m['cas_count_total'] / m['cycles']
        cache_dominance = 1 - (m['LLC_misses'] / m['LLC_loads'])
        
        # Aggressor skoru
        score = (
            llc_intensity * 0.30 +
            imc_intensity * 0.40 +
            cache_dominance * 0.30
        )
        scores[wl_id] = score
        
    return max(scores, key=scores.get)
