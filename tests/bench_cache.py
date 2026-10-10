"""Alternating two prompt families (router-like / chat-like), as Dork does: are prompt caches reused?"""
import httpx, time
A = "You are the decision step. " + "Rule about choosing actions. " * 150
B = "You are Dork, Hari's assistant. " + "Personality and memory line. " * 150
def call(sysmsg, user):
    r = httpx.post("http://127.0.0.1:11434/api/chat", json={"model": "qwen2.5:3b", "stream": False, "keep_alive": "24h",
        "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}],
        "options": {"num_ctx": 8192, "num_predict": 8, "temperature": 0}}, timeout=300).json()
    return r["prompt_eval_count"], r["prompt_eval_duration"] / 1e9, r["total_duration"] / 1e9
for i in range(3):
    for name, s in (("router", A), ("chat", B)):
        n, pe, tot = call(s, f"message {i}")
        print(f"round {i} {name:6s}: evaluated {n:5d} prompt tokens, {tot:5.1f}s total")
