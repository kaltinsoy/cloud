def estimate_cache_dominance(metrics):
	llc_intensity = metrics['LLC_loads'] / metrics['cycles']
	miss_rate = metrics['LLC_misses'] / metrics['LLC_loads']
	dominance_score = llc_intensity * (1 - miss_rate)
	return dominance_score
