"""Command step: does the brain pick the right action for spoken commands? (decision only, nothing executed)"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DORK_DATA"] = os.path.join(os.path.dirname(__file__), "_data")
from dork import assistant

CASES = [
    ("Open VS Code", "open_app", "code"), ("launch spotify for me", "open_app", "spotify"),
    ("open youtube", "open_website", "youtube"), ("go to github dot com", "open_website", "github"),
    ("open my downloads folder", "open_folder", "download"), ("close Chrome", "close_window", "chrome"),
    ("close this", "close_window", "this"), ("minimize that window", "minimize_window", ""),
    ("switch to notepad", "switch_to_window", "notepad"), ("maximize the calculator", "maximize_window", "calc"),
    ("turn the volume up", "media", "volume_up"), ("pause the music", "media", ""),
    ("how much battery do I have", "system_info", ""), ("remind me in 10 minutes to call mom", "set_timer", ""),
    ("open the chat", "open_chat", ""), ("show me the chat window", "open_chat", ""),
    ("open settings", "open_settings", ""), ("what do you remember about me", "show_memories|chat", ""),
    ("that's all, go to sleep", "go_to_sleep", ""), ("thanks Dork, bye", "go_to_sleep", ""),
    ("learn my voice", "learn_voice", ""), ("this is my sister Priya, learn her voice", "learn_voice", "priya"),
    ("who am I?", "who_is_speaking", ""), ("lock yourself", "lock", ""),
    ("what's the latest news about ISRO", "look_up", "isro"), ("what's the weather in Chennai tonight", "look_up", ""), ("how was your day", "chat", ""),
    ("what's 15 percent of 2400", "chat", ""),
    # held out: phrasings NOT in the prompt's examples
    ("how's your morning going", "chat", ""), ("what's 20 times 35", "chat", ""), ("any news about Tesla today", "look_up", ""),
    ("bring up spotify", "switch_to_window|open_app", "spotify"), ("show me our conversation", "open_chat", ""),
    ("can you close this window", "close_window", ""), ("make it louder", "media", "volume_up"),
    ("start calculator", "open_app", "calc"), ("what did we talk about earlier", "chat|look_up", ""), ("tell me a joke", "chat", ""),
]

async def main():
    ok = 0; t = time.time(); speaker = {"name": "Hari", "role": "owner", "score": 0.9}
    for text, want, tgt in CASES:
        d = await assistant.decide(text, speaker)
        good = d["action"] in want.split("|") and (not tgt or tgt in (d.get("target") or "").lower())
        ok += good
        print(("PASS " if good else "FAIL ") + f"{text!r} -> {d['action']} {d.get('target')!r} {d.get('seconds') or ''}")
    print(f"commands: {ok}/{len(CASES)} correct, {(time.time() - t) / len(CASES):.1f}s per decision")
asyncio.run(main())
