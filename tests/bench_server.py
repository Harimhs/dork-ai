"""Speed of a llama.cpp server (OpenAI API): writing tok/s on a few prompts, from server timings."""
import httpx, sys
URL = sys.argv[1]
P = ["Write a short paragraph about why the monsoon matters for farmers in Tamil Nadu.",
     "List five quick tips for staying focused while working from home, one line each.",
     "Explain what a voice assistant does, in four sentences."]
tot_n = tot_t = 0
for p in P:
    r = httpx.post(f"{URL}/completion", json={"prompt": f"<|im_start|>user\n{p}<|im_end|>\n<|im_start|>assistant\n",
                   "n_predict": 120, "temperature": 0, "cache_prompt": False}, timeout=900).json()
    t = r["timings"]; tot_n += t["predicted_n"]; tot_t += t["predicted_ms"] / 1000
    extra = f", draft accepted {t.get('draft_n_accepted', '-')}/{t.get('draft_n', '-')}" if "draft_n" in t else ""
    print(f"  wrote {t['predicted_n']} tok at {t['predicted_per_second']:.1f} tok/s{extra}")
print(f"  AVERAGE writing speed: {tot_n / tot_t:.1f} tok/s")
