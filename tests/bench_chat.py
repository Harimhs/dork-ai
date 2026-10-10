"""Time text-chat replies (first word + full reply) through the normal chat API."""
import json, os, sys, time
import httpx
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = httpx.Client(base_url="http://localhost:7777", cookies={"dork_token": open(os.path.join(ROOT, "data", "local-app.token")).read().strip()}, timeout=600)
conv = H.post("/api/conversations", json={"title": "speed test"}).json()["id"]
for text in sys.argv[1:]:
    t0 = time.time(); first = None; out = ""
    with H.stream("POST", f"/api/conversations/{conv}/chat", json={"message": text, "web": "auto"}) as r:
        for line in r.iter_lines():
            if not line: continue
            e = json.loads(line)
            if e["type"] == "token":
                first = first or time.time() - t0; out += e["text"]
    print(f"first word {first or 0:5.1f}s | full {time.time() - t0:5.1f}s | {text!r} -> {out[:140]!r}")
H.delete(f"/api/conversations/{conv}")  # remove the test thread
