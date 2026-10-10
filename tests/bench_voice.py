"""Time voice-assistant turns end to end (typed into the assistant, reply text captured from events)."""
import json, os, sys, threading, time
import httpx
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
token = open(os.path.join(ROOT, "data", "local-app.token")).read().strip()
H = httpx.Client(base_url="http://localhost:7777", cookies={"dork_token": token}, timeout=300)
events = []
def listen():
    with httpx.Client(base_url="http://localhost:7777", cookies={"dork_token": token}, timeout=None) as c:
        with c.stream("GET", "/api/assistant/events") as r:
            for line in r.iter_lines():
                if line.startswith("data: "): events.append((time.time(), json.loads(line[6:])))
threading.Thread(target=listen, daemon=True).start()
while not any(e["type"] == "hello" for _, e in events): time.sleep(0.2)
client = next(e["client"] for _, e in events if e["type"] == "hello")
for text in sys.argv[1:]:
    n = len(events); t0 = time.time()
    r = H.post("/api/assistant/utterance", json={"text": text, "client": client}).json()
    time.sleep(0.5)
    says = [(t, e["text"]) for t, e in events[n:] if e["type"] == "say"]
    first = f"{says[0][0] - t0:4.1f}s" if says else "  — "
    print(f"first words {first} | done {time.time() - t0 - 0.5:5.1f}s | {r.get('action')!s:12s} | {text!r} -> {' '.join(x for _, x in says)[:150]!r}")
