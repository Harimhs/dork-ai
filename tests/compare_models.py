"""Compare local brains (e.g. qwen2.5:3b vs qwen2.5:1.5b) on the same voice commands and chat questions.
Usage: python tests/compare_models.py qwen2.5:3b qwen2.5:1.5b   (copy data/config.json to tests/_data first)"""
import asyncio, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DORK_DATA"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_data")
from dork import assistant, context, llm
from dork.config import CFG
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_commands import CASES

CHAT = [  # (question, check: regex the answer must match)
    ("What's 15% of 2400? Just the number.", r"\b360\b"),
    ("What is 17 times 23?", r"\b391\b"),
    ("If I have 3 meetings of 45 minutes each, how many hours is that in total?", r"2\.25|2 hours (and )?15|two and a quarter|2¼"),
    ("What's the capital of Australia?", r"Canberra"),
    ("Who wrote the Thirukkural?", r"Thiruvalluvar"),
    ("Which is bigger, 9.11 or 9.9?", r"9\.9\b(?!\d)"),
    ("Name the planet closest to the sun.", r"Mercury"),
    ("Reply with exactly one word: what colour is the sky on a clear day?", r"^\W*blue\W*$"),
    ("Translate 'thank you' to Tamil.", r"நன்றி|nandri|nanri"),
    ("Spell 'Dork' backwards.", r"krod"),
    ("Who built you?", r"Hari"),
    ("Are you just a text-based AI?", r"^(?!.*(just a text|text-based ai|language model)).*$"),
]

async def run(model):
    CFG["ollama_model"] = model
    speaker = {"name": "Hari", "role": "owner", "score": 0.9}
    ok = 0; t0 = time.time()
    for text, want, tgt in CASES:
        d = await assistant.decide(text, speaker)
        ok += d["action"] in want.split("|") and (not tgt or tgt in (d.get("target") or "").lower())
    cmd_time = (time.time() - t0) / len(CASES)
    system = context.PERSONA.format(version="nano-2", creator=CFG["creator_name"], user="Name: Hari")
    good = 0; t0 = time.time(); answers = []
    for q, rx in CHAT:
        a = (await llm.complete(system, q, max_tokens=120)).strip()
        hit = bool(re.search(rx, a, re.I | re.S)); good += hit
        answers.append((hit, q, a.replace("\n", " ")[:110]))
    chat_time = (time.time() - t0) / len(CHAT)
    return ok, cmd_time, good, chat_time, answers

async def main():
    res = {}
    for m in sys.argv[1:]:
        res[m] = await run(m)
        ok, ct, good, cht, _ = res[m]
        print(f"[done] {m}: commands {ok}/{len(CASES)} in {ct:.1f}s each | chat {good}/{len(CHAT)} in {cht:.1f}s each", flush=True)
    print("\n=== SUMMARY ===")
    for m, (ok, ct, good, cht, _) in res.items():
        print(f"{m:16s} commands {ok}/{len(CASES)} correct, {ct:4.1f}s each | chat {good}/{len(CHAT)} correct, {cht:4.1f}s each")
    print("\n=== CHAT ANSWERS ===")
    models = list(res)
    for i, (q, _) in enumerate(CHAT):
        print(f"Q: {q}")
        for m in models:
            hit, _, a = res[m][4][i]
            print(f"   {'OK ' if hit else 'BAD'} {m:14s} {a!r}")
asyncio.run(main())
