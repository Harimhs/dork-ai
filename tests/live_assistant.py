"""Live end-to-end test against the running Dork server: typed and spoken phrases → real actions.
Opens/closes only Notepad, Calculator and a Downloads Explorer window."""
import base64, json, os, sys, threading, time
import httpx
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
token = open(os.path.join(ROOT, "data", "local-app.token")).read().strip()
H = httpx.Client(base_url="http://localhost:7777", cookies={"dork_token": token}, timeout=300)
events = []
def listen():
    with httpx.Client(base_url="http://localhost:7777", cookies={"dork_token": token}, timeout=None) as c:
        with c.stream("GET", "/api/assistant/events") as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
threading.Thread(target=listen, daemon=True).start()
for _ in range(50):
    time.sleep(0.2)
    if any(e["type"] == "hello" for e in events):
        break
client = next((e["client"] for e in events if e["type"] == "hello"), "")
assert client, "no event stream"
import re as _re
LOG = os.path.join(ROOT, "data", "dork.log")
def log_lines(): return open(LOG, encoding="utf-8", errors="ignore").read().splitlines()

from dork import desktop
def wins(name): return [w["title"] for w in desktop.windows() if name.lower() in (w["title"] + w["process"]).lower()]

def say(text=None, audio=None):
    n = len(events); t = time.time()
    body = {"client": client}
    if text: body["text"] = text
    if audio: body["audio_b64"] = base64.b64encode(open(audio, "rb").read()).decode(); body["mime"] = "audio/mpeg"
    r = H.post("/api/assistant/utterance", json=body).json()
    time.sleep(1.2)
    spoken = " ".join(e["text"] for e in events[n:] if e["type"] == "say")
    print(f"  [{time.time() - t:4.1f}s] {text or os.path.basename(audio)!r} -> {r.get('action')} | heard={r.get('heard')!r} | Dork: {spoken[:160]!r}")
    return r, spoken

results = []
def check(name, ok):
    results.append((name, ok)); print(("PASS " if ok else "FAIL ") + name)

r, s = say("open notepad"); time.sleep(2.5); check("open notepad", bool(wins("notepad")))
r, s = say("open calculator"); time.sleep(2.5); check("open calculator", bool(wins("calculator")))
r, s = say("switch to notepad"); time.sleep(1); check("switch to notepad (already open -> focus)", r.get("action") in ("switch_to_window", "open_app") and "Switched" in s)
r, s = say("minimize the calculator"); time.sleep(1)
check("minimize calculator", any(w["minimized"] for w in desktop.windows() if "calculator" in w["title"].lower()))
r, s = say("close the calculator"); time.sleep(2); check("close calculator", not wins("calculator"))
r, s = say("open my downloads folder"); time.sleep(2.5); check("open downloads folder", bool(wins("downloads")))
r, s = say("close the downloads window"); time.sleep(2); check("close downloads window", not wins("downloads"))
r, s = say("close notepad"); time.sleep(2); check("close notepad (graceful)", not wins("notepad") or "Notepad" in s)
r, s = say("close claude"); check("refuses to close Claude", bool(wins("claude")))
r, s = say("how much battery do I have left?"); check("battery via system info", any(ch.isdigit() for ch in s))
r, s = say("set a timer for 2 minutes to stretch"); check("timer", "2 minutes" in s or "Timer" in s)
r, s = say("hey, how's it going today?"); check("small talk in character", len(s) > 5 and "AI language model" not in s)
r, s = say("what did I just ask you to set a timer for?"); check("remembers context (timer for stretching)", "stretch" in s.lower())
r, s = say(audio="models/testvoices/en-IN-PrabhatNeural_3.mp3"); check("spoken question (recorded audio) transcribed", "weather" in (r.get("heard") or "").lower())
# echo test: Dork answers OUT LOUD on this PC's speakers (no client), while the mic is listening
n0 = len(log_lines())
H.post("/api/assistant/utterance", json={"text": "say something short and cheerful about Mondays", "client": ""})
time.sleep(25)
new = [l for l in log_lines()[n0:] if "dork.assistant" in l]
phantom = [l for l in new if "command" in l and "Mondays" not in l]
print("  echo log:", *new[-6:], sep="\n    ")
check("no phantom commands from Dork hearing itself", not phantom)
print(f"\n{sum(ok for _, ok in results)}/{len(results)} live checks passed")
