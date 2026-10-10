"""A realistic multi-turn voice conversation on the LOCAL brain, in-process with a throwaway database:
per turn, when Dork would start speaking, and how many tokens the brain had to re-read."""
import asyncio, logging, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DORK_DATA"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_turns")
os.makedirs(os.environ["DORK_DATA"], exist_ok=True)
import shutil; shutil.copy(os.path.join(os.path.dirname(os.environ["DORK_DATA"]), "..", "data", "config.json"), os.environ["DORK_DATA"])
logging.basicConfig(level=logging.WARNING)
from dork import assistant, llm
from dork.config import CFG
CFG["voice_brain"] = "local"
from dork import engine; print("C++ engine:", engine.start())
brain_log = []
class H(logging.Handler):
    def emit(self, r):
        if "local brain: read" in r.getMessage(): brain_log.append(r.getMessage())
lg = logging.getLogger("dork.llm"); lg.setLevel(logging.INFO); lg.addHandler(H())
spoken = []
said_text = []
async def fake_say(text, to=None, store=True):
    spoken.append(time.time()); said_text.append(text)
    if store: assistant._store("assistant", text)
assistant.say = fake_say
TURNS = sys.argv[1:] or ["I just got back from work, long day.", "Yeah, the meeting ran late again.", "Any tips to switch off after work?",
         "I like music. What should I listen to tonight?", "Something calm, maybe instrumental.", "Nice. And what should I eat?",
         "I have rice and eggs at home.", "Sounds good, thanks."]
async def main():
    for i, t in enumerate(TURNS):
        spoken.clear(); said_text.clear(); brain_log.clear(); t0 = time.time()
        await assistant.handle(__import__("numpy").zeros(0, "float32"), source="device", to="test", device_trusted=True, text=t)
        first = f"{spoken[0] - t0:5.1f}s" if spoken else "   - "
        print(f"turn {i + 1}: first words {first} | {chr(32).join(said_text)[:110]!r} | {chr(32).join(m.replace('local brain: ', '') for m in brain_log)}")
asyncio.run(main())
