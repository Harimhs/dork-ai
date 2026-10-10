"""Raw local-brain speed: prompt reading (tok/s) and writing (tok/s), same prompts every run."""
import httpx, sys, time
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11434"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "qwen2.5:3b"
long_sys = "You are Dork, Hari's personal assistant. " + ("Background note about Hari's life, work and projects. " * 90)
runs = []
for i, (sysmsg, q, n) in enumerate([(long_sys, "Tell me a two-sentence story about rain.", 60),
                                    (long_sys + " x", "Explain in three sentences why the sky is blue.", 80)]):
    r = httpx.post(f"{URL}/api/chat", json={"model": MODEL, "stream": False, "keep_alive": "24h",
        "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": q}],
        "options": {"num_ctx": 8192, "num_predict": n, "temperature": 0, "num_thread": 8}}, timeout=900).json()
    pr, pd = r["prompt_eval_count"], r["prompt_eval_duration"] / 1e9
    gr, gd = r["eval_count"], r["eval_duration"] / 1e9
    print(f"run {i}: read {pr} tok at {pr/pd:5.1f} tok/s | wrote {gr} tok at {gr/gd:4.1f} tok/s | load {r['load_duration']/1e9:.1f}s")
