"""A spoken conversation with Dork, the way a phone sends it: speech audio -> Dork -> what it heard, did, said, and when."""
import asyncio, base64, json, os, sys, threading, time
import edge_tts, httpx
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
VOICE = os.environ.get("VOICE", "en-IN-PrabhatNeural")
async def tts(text):
    data = b""
    async for c in edge_tts.Communicate(text, VOICE).stream():
        if c["type"] == "audio": data += c["data"]
    return data
for line in sys.argv[1:]:
    audio = asyncio.run(tts(line))
    n = len(events); t0 = time.time()
    r = H.post("/api/assistant/utterance", json={"audio_b64": base64.b64encode(audio).decode(), "mime": "audio/mpeg", "client": client}).json()
    time.sleep(0.4)
    says = [(t, e["text"]) for t, e in events[n:] if e["type"] == "say"]
    first = f"{says[0][0] - t0:4.1f}s" if says else "  -  "
    print(f"\nYOU SAID : {line}\nDORK HEARD: {r.get('heard')!r}  (voice: {(r.get('speaker') or {}).get('name') or 'unknown'})")
    print(f"ACTION   : {r.get('action')}   | first words {first}, done {time.time() - t0 - 0.4:4.1f}s")
    print(f"DORK SAID: {' '.join(x for _, x in says)}")
