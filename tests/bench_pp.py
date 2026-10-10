"""Prompt reading speed (tok/s) on a ~1,200-token prompt, uncached."""
import httpx, sys, time
p = "Background note about Hari's life, work and projects. " * 110
r = httpx.post(sys.argv[1] + "/completion", json={"prompt": p + str(time.time()), "n_predict": 1, "cache_prompt": False}, timeout=900).json()
t = r["timings"]; print(f"  reading: {t['prompt_n']} tok at {t['prompt_per_second']:.1f} tok/s")
