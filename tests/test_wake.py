"""Wake detection: is this phrase Hari calling Dork? (semantic, no word list). Needs Ollama running."""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DORK_DATA"] = os.path.join(os.path.dirname(__file__), "_data")
from dork import assistant

WAKE = ["Dork, wake up.", "Wake up Dork.", "Hey Dork.", "Dork, are you there?", "Doc, wake up.", "Hey Dark, you up?",
        "Dork, open Chrome.", "Yo Dork, I need you.", "Dork you awake buddy?", "Okay Dork, listen to me."]
NOT = ["He's such a dork, honestly.", "Don't be a dork about it.", "What time is the meeting tomorrow?",
       "Can you pass me the charger?", "It's getting dark outside.", "I left the door open.",
       "I'm going to work now, see you.", "That movie was so dorky.", "Did you call mom?", "My friend called me a dork today."]

async def main():
    ok = 0; t = time.time()
    for p in WAKE:
        r = await assistant.is_wake(p); ok += r; print(("PASS " if r else "MISS ") + p)
    for p in NOT:
        r = await assistant.is_wake(p); ok += not r; print(("PASS " if not r else "FALSE-WAKE ") + p)
    print(f"wake detection: {ok}/{len(WAKE) + len(NOT)} correct, {(time.time() - t) / (len(WAKE) + len(NOT)):.1f}s per phrase")
asyncio.run(main())
