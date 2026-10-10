"""The new-thread greeting: written by Dork from what's been going on lately, not a stock line.

Generating takes a few seconds on a local model, so the result is cached per part of the day and
refreshed in the background after new messages; the UI shows a simple line until it's ready."""

import datetime
import json
import logging
import random
import re

from . import db, llm, memory

log = logging.getLogger("dork.greeting")

PARTS = (("night", 0, 5), ("morning", 5, 12), ("noon", 12, 16), ("evening", 16, 21), ("night", 21, 24))

FALLBACK = {
    "morning": ["Morning, {name}.", "Fresh start, {name}.", "Up and running, {name}."],
    "noon": ["Afternoon, {name}.", "Midday check-in, {name}.", "Back at it, {name}."],
    "evening": ["Evening, {name}.", "Winding down, {name}?", "Good to see you, {name}."],
    "night": ["Still up, {name}?", "Late one, {name}.", "Night shift, {name}?"],
}

PROMPT = """You write the one-line greeting at the top of a new chat in Dork, {name}'s personal assistant.
It is {when} — {part}. Only use wording that fits {part}. Make it feel like a friend who remembers what {name} has been up to — mention ONE specific recent
thing from the context naturally (a project, a person, a plan), fitting the time of day.

Rules: max 14 words. Warm, a little witty, never cheesy. Not a question about how you can help. No emojis.
Don't start with "Hello" or "Hi". Don't invent facts beyond the context.
Also give 3 short suggestions (max 6 words each), written the way {name} would type them, for what they might
want to do next given the context."""

SCHEMA = {
    "type": "object",
    "properties": {"line": {"type": "string"}, "suggestions": {"type": "array", "items": {"type": "string"}}},
    "required": ["line", "suggestions"],
}


def part_of_day(now: datetime.datetime | None = None) -> str:
    h = (now or datetime.datetime.now()).hour
    return next(p for p, start, end in PARTS if start <= h < end)


def _name() -> str:
    p = memory.get_profile()
    return p["call_me"] or p["name"] or "there"


def _cache_key() -> str:
    last = db.one("SELECT MAX(id) AS id FROM messages")["id"] or 0
    return f"{datetime.date.today()}|{part_of_day()}|{last // 6}"  # refresh every few new messages


def _fallback() -> dict:
    part = part_of_day()
    line = random.Random(str(datetime.date.today()) + part).choice(FALLBACK[part]).format(name=_name())
    return {"part": part, "line": line, "suggestions": [], "pending": True}


def current() -> dict:
    """The greeting to show now: cached if fresh, else a simple line (and pending=True)."""
    cached = json.loads(db.get_meta("greeting") or "{}")
    if cached.get("key") == _cache_key():
        return {"part": part_of_day(), "line": cached["line"], "suggestions": cached["suggestions"], "pending": False}
    return _fallback()


_busy = False


async def refresh() -> None:
    global _busy
    if _busy:
        return
    _busy = True
    try:
        await _refresh()
    finally:
        _busy = False


async def _refresh() -> None:
    key = _cache_key()
    cached = json.loads(db.get_meta("greeting") or "{}")
    if cached.get("key") == key:
        return
    titles = [r["title"] for r in db.q("SELECT title FROM conversations ORDER BY updated_at DESC LIMIT 5")]
    recent = [r["content"][:160] for r in db.q("SELECT content FROM messages WHERE role='user' ORDER BY id DESC LIMIT 6")]
    facts = [f"[{r['path'] or r['category']}] {r['content']}" for r in
             db.q("SELECT content, path, category FROM memories ORDER BY importance DESC, updated_at DESC LIMIT 10")]
    if not (titles or recent or facts):
        return  # nothing to be personal about yet — the simple line is right
    now = datetime.datetime.now()
    name = _name()
    context = ("RECENT CHATS: " + "; ".join(titles) + "\nTHEIR RECENT MESSAGES:\n" + "\n".join(f"- {m}" for m in recent)
               + "\nWHAT DORK REMEMBERS:\n" + "\n".join(f"- {f}" for f in facts))
    try:
        raw = await llm.complete(PROMPT.format(name=name, when=f"{now:%A}, {now:%H:%M}", part=part_of_day(now).upper()),
                                 context, schema=SCHEMA, max_tokens=300)
        data = json.loads(raw)
        line = " ".join(str(data.get("line", "")).split())[:120].strip('"')
        suggestions = [" ".join(str(s).split())[:48] for s in data.get("suggestions", []) if str(s).strip()][:3]
    except Exception as e:  # noqa: BLE001 — keep the simple greeting
        log.info("greeting failed: %s", e)
        return
    # Small models sometimes say "Good morning" at midnight: reject a line with the wrong time of day.
    wrong = {"morning": r"evening|night|afternoon", "noon": r"morning|evening|night",
             "evening": r"morning|afternoon", "night": r"morning|afternoon|evening"}[part_of_day(now)]
    if line and re.search(rf"\b({wrong})\b", line, re.I):
        log.info("greeting rejected (wrong time of day): %s", line)
        return
    if line:
        db.set_meta("greeting", json.dumps({"key": key, "line": line, "suggestions": suggestions}))
