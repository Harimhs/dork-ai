"""Streamed voice turns on the local brain: when would Dork start speaking, and does it pick the right action?"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DORK_DATA"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_data")
from dork import assistant, engine
from dork.config import CFG
CFG["voice_brain"] = os.environ.get("BRAIN", "local")
CFG["local_engine"] = os.environ.get("ENGINE", CFG.get("local_engine"))
print("engine:", CFG["local_engine"], engine.start())
spoken = []
async def fake_say(text, to=None, store=True):
    spoken.append((time.time(), text))
assistant.say = fake_say
CASES = ["tell me a fun fact about octopuses", "what should I cook tonight, something quick", "give me a tip to sleep better", "tell me a fun fact about octopuses", "what should I cook tonight, something quick", "give me a tip to sleep better"]
async def main():
    for t in CASES:
        spoken.clear(); t0 = time.time()
        d = await assistant.decide_stream(t, {"name": "Hari", "role": "owner", "score": 0.9}, "test")
        first = f"{spoken[0][0] - t0:4.1f}s" if spoken else "  -  "
        print(f"first words {first} | done {time.time() - t0:4.1f}s | {d['action']:10s} {d.get('target','')!r:22s} {d.get('seconds') or ''} | {d['reply'][:90]!r}")
asyncio.run(main())
