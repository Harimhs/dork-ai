"""Fast path: common commands decided in under a second; anything unclear must go to the brain (None)."""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DORK_DATA"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_data")
from dork import assistant
CASES = [  # (phrase, expected action or None = must go to the brain)
    ("open chrome","open_app|switch_to_window"), ("Dork, open VS Code please","open_app|switch_to_window"), ("launch the calculator","open_app|switch_to_window"),
    ("close this", "close_window"), ("minimize this window", "minimize_window"), ("switch to claude","open_app|switch_to_window"),
    ("turn the volume up", "media"), ("make it quieter", "media"), ("pause the music", "media"), ("next song", "media"),
    ("open the chat", "open_chat"), ("thanks, that's all", "go_to_sleep"), ("what time is it", "system_info"),
    ("how's my battery", "system_info"), ("remind me in 5 minutes to drink water", "set_timer"),
    ("set a timer for 20 minutes", "set_timer"),
    # held out, not like any example
    ("could you start notepad","open_app|switch_to_window"), ("bring the calculator up", "switch_to_window|open_app"), ("shut this", "close_window"),
    ("louder please", "media"), ("skip this song", "media"),
    # must NOT take the fast path
    ("don't open chrome", None), ("chrome is really slow today", None), ("what's the latest news on ISRO", None),
    ("tell me a joke", None), ("open spotfy", None), ("how are you doing", None), ("how's it going", None), ("how's everything going today", None), ("hey what's up", None), ("open my favourite app", None),
    ("I'm going to sleep now, good night", None), ("what time does the mall close", None), ("I need more time to finish this", None),
    ("close the deal with the client tomorrow", None), ("my volume of work is crazy", None), ("open up to me, how do you feel", None),
]
async def main():
    await assistant._fast_vectors()
    ok = 0; worst = 0
    for text, want in CASES:
        t = time.time(); d = await assistant.fast_intent(text); dt = time.time() - t; worst = max(worst, dt)
        got = d["action"] if d else None
        good = (got == want) if want is None else (got in want.split("|")); ok += good
        print(f"{'PASS' if good else 'FAIL'} {dt:4.2f}s {text!r} -> {got} {(d or {}).get('target', '')!r}")
    print(f"fast path: {ok}/{len(CASES)} correct, slowest {worst:.2f}s")
asyncio.run(main())
