"""Dork the voice assistant: always listening on this PC, wakes when Hari addresses it, acts on commands.

    mic ─ VAD ─ phrase ─┬─ voiceprint: who? (only Hari's voice can wake Dork / control the PC, once enrolled)
                        └─ Whisper: what?  ─┬─ asleep: is this Hari calling Dork?  (semantic, no wake-word list)
                                            └─ awake:  brain picks an action (JSON) → do it → speak

The UI (web/assistant.html, the 3D reactor) is only a face: it follows the events this module
broadcasts and plays Dork's voice. Phones/browsers can also send recorded phrases (utterance())."""

import asyncio
import datetime
import difflib
import json
import logging
import queue
import random
import re
import secrets
import threading
import time

import numpy as np

from . import VERSION, agent, context, db, desktop, hearing, llm, memory, voice
from .config import CFG

log = logging.getLogger("dork.assistant")

# ---------------------------------------------------------------- events → UI

_clients: dict[str, dict] = {}   # client id → {"q": asyncio.Queue, "host": bool}
_loop: asyncio.AbstractEventLoop | None = None


def subscribe(host: bool) -> tuple[str, asyncio.Queue]:
    cid = secrets.token_hex(6)
    _clients[cid] = {"q": asyncio.Queue(maxsize=400), "host": host}
    return cid, _clients[cid]["q"]


def unsubscribe(cid: str) -> None:
    _clients.pop(cid, None)


def hosts() -> int:
    return sum(1 for c in _clients.values() if c["host"])


def emit(ev: dict, to: str | None = None) -> None:
    """Send an event to every UI (or one client). Safe to call from any thread."""
    def put():
        for cid, c in list(_clients.items()):
            if to and cid != to and not (to == "host" and c["host"]):
                continue
            try:
                c["q"].put_nowait(ev)
            except asyncio.QueueFull:
                pass
    if _loop and _loop.is_running():
        _loop.call_soon_threadsafe(put)


# ---------------------------------------------------------------- state

class S:
    state = "sleeping"          # sleeping | awake | thinking | speaking
    awake_until = 0.0
    speaking_until = 0.0        # the UI reports when it's playing Dork's voice
    pending: dict | None = None  # a question Dork asked (confirmation)
    enrolling: dict | None = None
    locked = False              # guard mode: needs a voice check (liveness phrase) to wake
    challenge: str | None = None
    last_speaker: dict = {}
    spoke: list = []            # [start, end] times Dork's voice was playing (for echo rejection)
    said: list = []             # [(time, text)] what Dork said lately
    armed_until = 0.0
    heard_conf = 0.0            # how sure the hearing was about the current phrase (Whisper log-prob; 0 = sure)
    host_mic_until = 0.0        # the assistant window is listening (echo-cancelled): it owns the conversation
    quick = False               # the current command came from the fast path           # the PC mic takes follow-ups without "Dork" until then (after a voice exchange)
    timers: dict[str, asyncio.Task] = {}
    ears_ok = False
    ears_error = ""


def set_state(state: str) -> None:
    S.state = state
    if state in ("awake", "speaking", "thinking"):
        S.awake_until = time.time() + CFG["assistant_sleep_after"]
    emit({"type": "state", "state": state, "locked": S.locked})


def snapshot() -> dict:
    return {"state": S.state, "locked": S.locked, "ears": S.ears_ok, "ears_error": S.ears_error,
            "enrolling": bool(S.enrolling), "hearing": hearing.ready(), "hosts": hosts()}


# ---------------------------------------------------------------- the voice conversation (kept in history)

def voice_conversation() -> dict:
    cid = db.get_meta("voice_conversation")
    conv = db.one("SELECT * FROM conversations WHERE id=?", (cid,)) if cid else None
    if not conv:
        cid = secrets.token_hex(8)
        db.x("INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?,?,?,?)",
             (cid, "Voice", db.now(), db.now()))
        db.set_meta("voice_conversation", cid)
        conv = db.one("SELECT * FROM conversations WHERE id=?", (cid,))
    return conv


def _store(role: str, text: str, device: str = "voice") -> None:
    conv = voice_conversation()
    db.x("INSERT INTO messages(conversation_id, role, content, device, created_at) VALUES(?,?,?,?,?)",
         (conv["id"], role, text, device, db.now()))
    db.x("UPDATE conversations SET updated_at=? WHERE id=?", (db.now(), conv["id"]))


def _recent(n: int = 6) -> list[dict]:
    conv = voice_conversation()
    rows = db.q("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", (conv["id"], n))
    return list(reversed(rows))


# ---------------------------------------------------------------- speaking

async def say(text: str, to: str | None = None, store: bool = True) -> None:
    """Dork speaks: the UI plays it (or, with no assistant window open, this PC's speakers do)."""
    text = text.strip()
    if not text:
        return
    if store:
        _store("assistant", text)
    _remember_said(text)
    S.speaking_until = time.time() + 1.5 + len(text) / 14  # rough; the UI corrects it
    S.spoke.append([time.time(), S.speaking_until])
    emit({"type": "say", "text": text}, to=to or "host")
    if not to and not hosts():
        await _play_locally(text)


async def _play_locally(text: str) -> None:
    for sentence in voice.sentences(voice.speakable(text)) or [text]:
        out = await voice.speak(sentence, False)
        if not out:
            continue
        try:
            samples = hearing.decode(out[0])
            import sounddevice as sd

            start = time.time()
            S.speaking_until = start + len(samples) / hearing.RATE + 0.4
            S.spoke.append([start, S.speaking_until])
            await asyncio.to_thread(lambda: (sd.play(samples, hearing.RATE), sd.wait()))
            S.spoke[-1][1] = time.time() + 0.3
        except Exception as e:  # noqa: BLE001
            log.info("local playback failed: %s", e)


def spoken(playing: bool) -> None:
    """The UI tells us when Dork's voice starts/stops (so Dork doesn't listen to itself)."""
    now = time.time()
    S.speaking_until = now + (30 if playing else 0.35)
    if playing:
        S.spoke.append([now, now + 30])
    elif S.spoke:
        S.spoke[-1][1] = now + 0.3
    if not playing and S.state != "sleeping":
        # Mid-conversation: a fresh moment to answer back without saying "Dork" — however long the reply took.
        S.armed_until = max(S.armed_until, now + FOLLOW_UP)
    if playing:
        set_state("speaking")
    elif S.state == "speaking":
        set_state("awake")


# ---------------------------------------------------------------- wake detection (semantic, not a word list)

WAKE_EXAMPLES = ["Dork wake up", "wake up Dork", "hey Dork", "Dork are you there", "Dork, you awake?",
                 "Dork I need you", "okay Dork", "Dork, come here", "yo Dork", "Dork, listen",
                 "Doc wake up", "hey Dorg, you there", "Dirk, are you awake"]  # how speech-to-text mishears it
NOT_WAKE_EXAMPLES = ["he is such a dork", "don't be a dork about it", "that movie was dorky", "I'm going to work now",
                     "what time is it", "pass me the water", "the door is open", "it's dark outside"]
_wake_vecs: tuple | None = None

WAKE_CHECK = """You decide whether a phrase picked up by a microphone is the user calling their voice assistant,
which is named Dork, to get its attention or give it a request (e.g. "Dork wake up", "hey Dork, open Chrome",
"Dork you there?"). Talking ABOUT someone being a dork, or speech not addressed to the assistant, is NOT.
Speech-to-text may mishear the name as "Doc", "Dark", "Dorg", "Talk" or "Dirk"."""


def _cos(a, b) -> float:
    a, b = np.asarray(a), np.asarray(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


# A wake call has to contain the name — or how speech-to-text tends to mishear it.
NAME_HEARD = re.compile(r"\b(dork|dorks|dorky|dorg|dirk|doc|dock|dark|dhork|torque|talk|duck)\b", re.I)


async def is_wake(text: str) -> bool:
    global _wake_vecs
    t = text.strip()
    if not t or len(t.split()) > 25 or not NAME_HEARD.search(t):
        return False
    try:
        if _wake_vecs is None:
            vecs = await llm.embed(WAKE_EXAMPLES + NOT_WAKE_EXAMPLES)
            if vecs:
                _wake_vecs = (vecs[:len(WAKE_EXAMPLES)], vecs[len(WAKE_EXAMPLES):])
        if _wake_vecs:
            v = (await llm.embed([t]) or [None])[0]
            if v is not None:
                pos = max(_cos(v, p) for p in _wake_vecs[0])
                neg = max(_cos(v, n) for n in _wake_vecs[1])
                log.info("wake? %r pos=%.2f neg=%.2f", t, pos, neg)
                if pos >= 0.86 and pos > neg + 0.04:
                    return True
                if pos < 0.6 or neg > pos + 0.05:
                    return False
        # In between (or no embeddings): ask the brain, constrained to a yes/no.
        raw = await llm.complete(WAKE_CHECK, f'Phrase: "{t}"', schema={
            "type": "object", "properties": {"calling_dork": {"type": "boolean"}}, "required": ["calling_dork"]},
            max_tokens=30)
        return bool(json.loads(raw).get("calling_dork"))
    except Exception as e:  # noqa: BLE001
        log.info("wake check failed: %s", e)
        return False


# ---------------------------------------------------------------- the command step: the brain picks an action

ACTIONS = ["greet", "chat", "look_up", "open_app", "open_website", "open_folder", "close_window", "minimize_window", "maximize_window",
           "switch_to_window", "media", "system_info", "set_timer", "open_chat", "open_settings", "show_memories",
           "go_to_sleep", "learn_voice", "forget_voice", "who_is_speaking", "lock", "yes", "no"]

COMMAND = """You are the command step of Dork, Hari's JARVIS-like voice assistant running on his Windows PC.
Decide what Dork should DO for the user's latest spoken words. Speech-to-text may contain small mistakes.

Actions:
- greet: they're only calling Dork or saying hello, with no other request ("Dork wake up", "hey Dork, you there?",
  "wake up buddy", "hi Dork").
- open_app: launch an installed program ("open VS Code", "launch Spotify"). target = app name.
- open_website: open a site in the browser ("open YouTube", "go to github"). target = full URL (https://...).
- open_folder: open a folder ("open my downloads"). target = folder name or path.
- close_window / minimize_window / maximize_window / switch_to_window: act on an open window. target = the app
  or window name; use "this" when they mean the window in front ("close this", "minimize that").
- media: target = one of play_pause, next, previous, volume_up, volume_down, mute. count = how many steps (volume).
- system_info: battery, CPU, memory, what's open.
- set_timer: a NEW timer/reminder. seconds = duration, target = only what it's for, never the duration
  ("remind me in 10 minutes to call mom" → 600, "call mom"; "timer for 5 minutes" → 300, "").
  Questions ABOUT earlier timers or anything said before are chat.
- open_chat: show the chat window (text view of the conversation). open_settings / show_memories: those screens.
- go_to_sleep: they're done ("that's all", "go to sleep", "hide", "thanks Dork, bye").
- learn_voice: they want Dork to learn a voice. target = the person's name if it's someone else ("learn my sister
  Priya's voice" → "Priya"), else "".
- forget_voice: delete a stored voice. target = name.
- who_is_speaking: "who am I?", "do you recognise my voice?".
- lock: "lock yourself", "guard mode": Dork will need a voice check before it wakes again.
- yes / no: answering a question Dork just asked.
- look_up: ONLY when the answer must come from the internet right now: news, weather, prices, scores, recent
  events, or "search for / look up ...". target = search keywords.
- chat: everything else: small talk ("how was your day", "how are you"), opinions, advice, jokes, maths and
  calculations, the time/date, general knowledge, questions about Hari himself or what Dork remembers about him
  ("what do you remember about me"), and questions about what was said earlier.
- switch_to_window is for "switch to / go to / bring up X"; open_app is for "open / launch / start X".

Examples:
"how was your day" → chat | "how are you doing" → chat | "what do you remember about me" → chat
"what's 15 percent of 2400" → chat | "tell me a joke" → chat | "who are you" → chat
"what time is it" → chat (reply with the time from "Now") | "great" → chat | "thanks" → chat | "okay cool" → chat  (acknowledgements are never commands)
"latest news on ISRO" → look_up | "weather in Chennai tonight" → look_up | "bitcoin price" → look_up
"switch to notepad" → switch_to_window notepad | "open notepad" → open_app notepad
"open the chat" → open_chat | "show me the chat" → open_chat
"what did I ask you to remind me about?" → chat | "when is my timer?" → chat  (questions about earlier requests)
Pick chat when unsure. Only fill the fields the action needs."""

COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "target": {"type": "string"},
        "seconds": {"type": "integer"},
        "count": {"type": "integer"},
        "reply": {"type": "string"},
    },
    "required": ["action", "target", "reply"],  # seconds / count only when an action needs them (fewer tokens)
}

REPLY_RULES = """

reply = what Dork says OUT LOUD, as Dork (the personality above), in one or two short spoken sentences:
- for greet and chat: the actual answer. Warm, quick, a little witty, like a friend who's been around all day.
  Never customer-service lines ("How can I assist you today?", "How else can I help?") and never list what you
  can do. No markdown, no emojis.
- for every other action: leave it empty (Dork says what happened after doing it)."""


STREAM_FORMAT = """

Answer in EXACTLY this format and nothing else:
ACTION: <one action name from the list>
TARGET: <the target, or leave blank>
NUMBER: <seconds for set_timer, steps for media, or leave blank>
SAY: <only for greet and chat: what Dork says OUT LOUD, as Dork (the personality above), one to three short
natural spoken sentences. Warm, quick, a little witty, like a friend who's been around all day. Never
customer-service lines ("How can I assist you today?"), never list what you can do, no markdown, no emojis.
For every other action leave SAY blank: Dork says what happened after doing it.>"""


def _stream_system() -> str:
    """Stable across turns (persona + rules + format), so the brain's prompt cache stays warm."""
    persona = context.PERSONA.format(version=VERSION, creator=CFG["creator_name"], user=context._user_block())
    return persona + "\n\n" + COMMAND + STREAM_FORMAT


def _command_system() -> str:
    """Stable across turns (persona + rules), so the local brain's prompt cache stays warm."""
    persona = context.PERSONA.format(version=VERSION, creator=CFG["creator_name"], user=context._user_block())
    return persona + "\n\n" + COMMAND + REPLY_RULES


def _window(n: int = 8) -> list[dict]:
    """Recent voice turns, from a start point that only moves every n messages, so the prompt prefix
    (and the brain's cache) stays the same between most turns."""
    conv = voice_conversation()
    total = db.one("SELECT COUNT(*) AS c FROM messages WHERE conversation_id=?", (conv["id"],))["c"]
    start = max(0, (total - n) // n * n)
    return db.q("SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id LIMIT -1 OFFSET ?",
                (conv["id"], start))

PC_ACTIONS = {"open_app", "open_website", "open_folder", "close_window", "minimize_window", "maximize_window",
              "switch_to_window", "media", "set_timer", "learn_voice", "forget_voice", "lock"}


async def decide(text: str, speaker: dict) -> dict:
    """One brain call per voice turn: the action AND (for conversation) the spoken reply.
    Stable parts first (persona, rules, conversation so far), per-turn details last: cache-friendly."""
    screen, memories = await asyncio.gather(asyncio.to_thread(desktop.describe), memory.recall(text))
    convo = _window()
    if convo and convo[-1]["role"] == "user" and convo[-1]["content"] == text:
        convo = convo[:-1]  # the latest phrase is shown separately, at the end
    prompt = ("VOICE CONVERSATION SO FAR:\n"
              + ("\n".join(f"{'HARI' if m['role'] == 'user' else 'DORK'}: {m['content'][:300]}" for m in convo) or "(none)")
              + f"\n\n---\nNow: {datetime.datetime.now():%A %d %B %Y, %H:%M}\n"
              f"Window in front: {screen['in_front'] or 'none'}\nOther open windows: {', '.join(screen['open'][:8]) or 'none'}\n"
              + ("Things you remember that may matter: " + " | ".join(m["content"] for m in memories[:3]) + "\n" if memories else "")
              + (f"Dork just asked: {S.pending['said']}\n" if S.pending else "")
              + (f"The speaker is {speaker['name']}, not Hari.\n" if speaker.get("name") and speaker.get("role") != "owner" else "")
              + f"\nLATEST: {text}")
    try:
        llm.chat_waiting += 1
        with llm.cloud_brain():  # Gemini when configured (≈2 s), local brain as the fallback
            async with llm.exclusive():
                llm.chat_waiting -= 1
                raw = await llm.complete(_command_system(), prompt, schema=COMMAND_SCHEMA, max_tokens=160)
        d = json.loads(raw)
    except Exception as e:  # noqa: BLE001 — a broken decision step means: just talk
        log.info("command step failed: %s", e)
        d = {"action": "chat"}
    if d.get("action") not in ACTIONS:
        d["action"] = "chat"
    log.info("command %r → %s", text, d)
    return d


# ---------------------------------------------------------------- fast path: common commands in under a second
#
# The brain on this CPU needs ~10 s. Everyday commands ("open Chrome", "volume up", "close this") don't need it:
# a meaning match (embeddings, like wake detection) against example phrasings picks the intent, and the target
# must be a real installed app / open window found in what Hari said. Anything unclear goes to the brain.

FAST_INTENTS = {
    "open_app": ["open chrome", "launch spotify", "start vs code", "open the calculator for me", "can you open notepad",
                 "fire up word"],
    "close_window": ["close chrome", "close this window", "close that", "shut notepad", "close the calculator",
                     "can you close this"],
    "minimize_window": ["minimize this", "minimise chrome", "hide this window", "minimize that window"],
    "maximize_window": ["maximize this", "make chrome full screen", "maximise the window"],
    "switch_to_window": ["switch to chrome", "go to notepad", "bring up spotify", "show me vs code"],
    "media:volume_up": ["volume up", "turn it up", "make it louder", "increase the volume"],
    "media:volume_down": ["volume down", "turn it down", "make it quieter", "lower the volume"],
    "media:mute": ["mute", "mute the sound", "unmute"],
    "media:play_pause": ["pause the music", "play the music", "pause", "resume playing"],
    "media:next": ["next song", "skip this track", "next track"],
    "media:previous": ["previous song", "go back a track", "previous track"],
    "open_chat": ["open the chat", "show me the chat window", "open our conversation"],
    "go_to_sleep": ["go to sleep", "that's all, thanks", "bye dork", "you can rest now", "thanks, that's it"],
    "system_info": ["how much battery do I have", "what's my battery level", "how's the cpu doing"],
    "time": ["what time is it", "what's the time", "tell me the time", "what's today's date"],
    "set_timer": ["remind me in 10 minutes to call mom", "set a timer for 5 minutes", "timer for two minutes",
                  "remind me in an hour to take a break", "wake me up in 20 minutes"],
}
# Close in wording, different in meaning: if one of these is nearer, the brain decides.
FAST_NOT = ["don't open chrome", "I opened chrome yesterday", "chrome is so slow today", "why is the music so loud",
            "do you like music", "what's your favourite song", "I'm going to sleep now, good night",
            "remind me to close the window", "what time does the shop open", "how long will the battery last on a trip",
            "tell me about chrome", "the volume of a sphere", "how's it going", "how are you doing", "how's everything",
            "how's life", "how was your day", "what's up", "how have you been"]
_fast_vecs: dict | None = None
# What a web lookup sounds like; the brain's look_up is double-checked against these (small talk never searches).
LOOKUP_LIKE = ["what's the latest news about ISRO", "weather in Chennai today", "bitcoin price right now",
               "who won the match yesterday", "search for the best budget laptops", "look up the release date of GTA 6",
               "what's happening in the world today"]
CHAT_LIKE = ["okay cool", "how was your day", "thanks", "nice", "tell me a joke", "I'm tired", "what do you think"]
_lookup_vecs: tuple | None = None


async def _really_lookup(text: str) -> bool:
    """Is this phrase actually asking for fresh information from the internet? (meaning, not keywords)"""
    global _lookup_vecs
    try:
        if _lookup_vecs is None:
            vecs = await llm.embed(LOOKUP_LIKE + CHAT_LIKE)
            if not vecs:
                return True
            _lookup_vecs = (vecs[:len(LOOKUP_LIKE)], vecs[len(LOOKUP_LIKE):])
        v = (await llm.embed([_core(text)]) or [None])[0]
        if v is None:
            return True
        look = max(_cos(v, e) for e in _lookup_vecs[0])
        chat_ = max(_cos(v, e) for e in _lookup_vecs[1])
        log.info("lookup check %r: look %.2f chat %.2f", text, look, chat_)
        return look > chat_
    except Exception:  # noqa: BLE001
        return True
PRONOUN = {"this", "that", "it", "this window", "that window", "the window"}


async def _fast_vectors() -> dict | None:
    global _fast_vecs
    if _fast_vecs is None:
        phrases = [(k, p) for k, ps in FAST_INTENTS.items() for p in ps] + [("_not", p) for p in FAST_NOT]
        vecs = await llm.embed([p for _, p in phrases])
        if not vecs:
            return None
        _fast_vecs = {}
        for (k, _), v in zip(phrases, vecs):
            _fast_vecs.setdefault(k, []).append(np.asarray(v, dtype=np.float32))
    return _fast_vecs


def _target_in(text: str, kind: str) -> str | None:
    """The real app / window named in the phrase (tries 1-3 word spans), or a pronoun for the window in front."""
    # Dork's own name is how Hari addresses it, never the app he means ("Dork, open Notepad" -> Notepad)
    words = [w for w in re.findall(r"[a-z0-9.+#]+", _core(text).lower()) if w not in ("dork", "hey", "ok", "okay")]
    spans = [" ".join(words[i:i + n]) for n in (3, 2, 1) for i in range(len(words) - n + 1)]
    if kind != "open_app" and any(sp in PRONOUN for sp in spans):
        return "this" if desktop.current() else None
    best, score = None, 0.0
    for sp in spans:
        if len(sp) < 3:
            continue
        if kind == "open_app":
            a = desktop.find_app(sp)
            sc = desktop._score(desktop.ALIASES.get(sp, sp), a["name"]) if a else 0
            if a and sc > score:
                best, score = a["name"], sc
        else:
            for w in desktop.windows():
                if desktop.protected(w) and kind != "switch_to_window":
                    continue
                sc = desktop._window_score(sp, w)
                if sc > score:
                    best, score = sp, sc
    return best if score >= 0.85 else None


POLITE = re.compile(r"^(hey |okay |ok |yo )?dork[,.!]?\s*|\b(please|for me|can you|could you|would you)\b|[,.!?]", re.I)


CALLING_WORDS = {"dork", "doc", "dorg", "dirk", "dark", "hey", "hi", "hello", "okay", "ok", "yo", "you", "there",
                 "wake", "up", "awake", "are", "buddy", "listen", "i", "quote"}


def _only_calling(text: str) -> bool:
    """Nothing but the name / a hello ("Dork.", "hey Dork, you there?", "Dork, wake up") — no request in it."""
    return not [w for w in re.findall(r"[a-z']+", text.lower()) if w not in CALLING_WORDS]


def _core(text: str) -> str:
    """The command itself, without the name and politeness: "Dork, open VS Code please" -> "open VS Code"."""
    return re.sub(r"\s+", " ", POLITE.sub(" ", text)).strip() or text


NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
                "forty-five": 45, "sixty": 60, "half": 0.5}
UNIT = {"sec": 1, "second": 1, "min": 60, "minute": 60, "hour": 3600, "hr": 3600}


def _duration_in(text: str) -> tuple[int, str]:
    """"remind me in 5 minutes to drink water" -> (300, "drink water"). Sums "1 hour and 30 minutes"."""
    t = text.lower().replace("half an hour", "30 minutes")
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?|[a-z-]+)\s+(sec|second|min|minute|hour|hr)s?\b", t):
        n = float(num) if num[0].isdigit() else NUMBER_WORDS.get(num)
        if n:
            total += n * UNIT[unit]
    label = ""
    if m := re.search(r"\b(?:to|for|about)\s+(?!\d)([a-z][a-z' ]{2,40})$", t.strip(" .!?")):
        label = re.sub(r"\b(in|after) .*$", "", m.group(1)).strip()
        if re.search(r"\b(sec|second|min|minute|hour|hr)s?\b", label):
            label = ""
    return int(total), label


async def fast_intent(text: str) -> dict | None:
    if len(text.split()) > 16 or S.pending or S.enrolling:
        return None
    if not re.sub(r"\b(hey|hi|hello|yo|okay|ok|dork|there|are|you)\b|[^a-z]", "", text.lower()):
        return {"action": "greet", "target": "", "fast": True}  # just "Dork" / "hey Dork": a call, never "bye"
    try:
        vecs = await _fast_vectors()
        v = (await llm.embed([_core(text)]) or [None])[0]
    except Exception:  # noqa: BLE001
        return None
    if not vecs or v is None:
        return None
    v = np.asarray(v, dtype=np.float32)
    sims = {k: max(_cos(v, e) for e in es) for k, es in vecs.items()}
    neg = sims.pop("_not", 0)
    ranked = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)
    (kind, best), (kind2, second) = ranked[0], ranked[1]
    if {kind, kind2} == {"open_app", "switch_to_window"}:
        # Same outcome either way: opening an open app brings it forward, switching to a closed one opens it.
        second = ranked[2][1]
    # The bar can be moderate: an app/window action still has to find a real app or window in the phrase.
    clear = (best >= 0.74 and best - second >= 0.03) or (best >= 0.68 and best - second >= 0.12)  # or a clear winner
    if not clear or neg > best - 0.02:
        log.info("fast path: no (%s %.2f, next %.2f, not %.2f)", kind, best, second, neg)
        return None
    d = {"action": kind, "target": "", "fast": True}
    if kind.startswith("media:"):
        d = {"action": "media", "target": kind.split(":")[1], "count": 1, "fast": True}
    elif kind == "time":
        d = {"action": "system_info", "target": "time", "fast": True}
    elif kind == "set_timer":
        secs, label = _duration_in(text)
        if not secs:
            return None  # a timer, but no clear duration: the brain asks
        d = {"action": "set_timer", "seconds": secs, "target": label, "fast": True}
    elif kind in ("open_app", "close_window", "minimize_window", "maximize_window", "switch_to_window"):
        target = await asyncio.to_thread(_target_in, text, kind)
        if not target and kind == "switch_to_window":  # not open: then it's "open it"
            if target := await asyncio.to_thread(_target_in, text, "open_app"):
                d["action"] = "open_app"
        if not target:
            log.info("fast path: %s but no app/window found in %r", kind, text)
            return None
        d["target"] = target
    log.info("fast path %r → %s (%.2f)", text, d, best)
    return d




async def understand(text: str, speaker: dict, to: str | None) -> dict:
    """Fast path first; otherwise the brain, with a short filler if it's taking a while."""
    if d := await fast_intent(text):
        return d
    return await decide_stream(text, speaker, to)


ALIAS_ACTIONS = {"lookup": "look_up", "search": "look_up", "web_search": "look_up", "open": "open_app",
                 "launch": "open_app", "close": "close_window", "sleep": "go_to_sleep", "timer": "set_timer",
                 "reminder": "set_timer", "hello": "greet", "talk": "chat", "answer": "chat"}
FIELD_LINE = re.compile(r"^\s*(action|target|number|say|lookup|look_up|chat|greet)\s*:", re.I)

SENTENCE = re.compile(r"(.+?[.!?])(\s+|$)", re.S)
FIELD = re.compile(r"^\s*(ACTION|TARGET|NUMBER|SAY)\s*:\s*(.*)$", re.I)


def _turn_prompt(text: str, speaker: dict, screen: dict, memories: list[dict]) -> str:
    convo = _window()
    if convo and convo[-1]["role"] == "user" and convo[-1]["content"] == text:
        convo = convo[:-1]  # the latest phrase is shown separately, at the end
    return ("VOICE CONVERSATION SO FAR:\n"
            + ("\n".join(f"{'HARI' if m['role'] == 'user' else 'DORK'}: {m['content'][:300]}" for m in convo) or "(none)")
            + f"\n\n---\nNow: {datetime.datetime.now():%A %d %B %Y, %H:%M}\n"
            f"Window in front: {screen['in_front'] or 'none'}\nOther open windows: {', '.join(screen['open'][:8]) or 'none'}\n"
            + ("Things you remember that may matter: " + " | ".join(m["content"] for m in memories[:3]) + "\n" if memories else "")
            + (f"Dork just asked: {S.pending['said']}\n" if S.pending else "")
            + (f"The speaker is {speaker['name']}, not Hari.\n" if speaker.get("name") and speaker.get("role") != "owner" else "")
            + f"\nLATEST: {text}")


async def decide_stream(text: str, speaker: dict, to: str | None) -> dict:
    """One streamed brain call: the action comes first; for conversation, Dork starts speaking as soon as the
    first sentence is written (instead of after the whole reply). For PC actions the brain is stopped as soon
    as the action and target are known."""
    screen, memories = await asyncio.gather(asyncio.to_thread(desktop.describe), memory.recall(text))
    prompt = _turn_prompt(text, speaker, screen, memories)
    fields: dict[str, str] = {}
    buf, said, spoken_upto, raw = "", [], 0, ""
    started = time.time()
    try:
        llm.chat_waiting += 1
        with llm.cloud_brain():  # Gemini when available, the local brain otherwise
            async with llm.exclusive():
                llm.chat_waiting -= 1
                # low temperature: the action must be a decision, not a guess (the reply still reads naturally)
                async for kind, value in llm.chat_step(_stream_system(), "", [{"role": "user", "content": prompt}],
                                                       temperature=0.2):
                    if kind != "text":
                        continue
                    raw += value
                    buf += value
                    # complete lines: header fields
                    while "\n" in buf and "SAY" not in fields:
                        line, rest = buf.split("\n", 1)
                        m = FIELD.match(line)
                        if m and m.group(1).upper() == "SAY":  # the spoken text starts here: keep it in buf
                            fields["SAY"] = ""
                            buf = m.group(2) + " " + rest
                            break
                        buf = rest
                        if m:
                            fields[m.group(1).upper()] = m.group(2).strip()
                    if "SAY" not in fields and (m := FIELD.match(buf)) and m.group(1).upper() == "SAY":
                        fields["SAY"] = ""
                        buf = m.group(2)
                    action = (fields.get("ACTION") or "").lower().strip(" .")
                    if action and action not in ("chat", "greet") and ("NUMBER" in fields or "SAY" in fields):
                        break  # a PC action: everything needed is known, stop the brain now
                    if "SAY" in fields and action in ("chat", "greet", ""):
                        # speak each finished sentence right away
                        while (m := SENTENCE.match(buf, spoken_upto)) and len(m.group(1).strip()) > 1:
                            sentence = _clean_reply(m.group(1))
                            spoken_upto = m.end()
                            if FIELD_LINE.match(sentence):
                                continue
                            if sentence:
                                if not said:
                                    log.info("first words after %.1fs", time.time() - started)
                                said.append(sentence)
                                await say(sentence, to, store=False)
        if "SAY" in fields and (rest := _clean_reply(buf[spoken_upto:])):
            said.append(rest)
            await say(rest, to, store=False)
    except Exception as e:  # noqa: BLE001 — a broken brain step means: just talk
        log.info("streamed turn failed: %s", e)
        if not raw:
            return {"action": "chat"}
    action = (fields.get("ACTION") or "").lower().strip(" .")
    if not fields and (m := re.match(r"^\s*([a-z_ ]{3,20})\s*:\s*(.*)$", raw.strip(), re.I | re.S)):
        # "lookup: guitar advice" / "chat: ..." - the brain wrote ACTION: value on one line
        maybe = ALIAS_ACTIONS.get(m.group(1).strip().lower().replace(" ", "_"), m.group(1).strip().lower().replace(" ", "_"))
        if maybe in ACTIONS:
            action, fields = maybe, {"ACTION": maybe, "TARGET": m.group(2).split("\n")[0].strip()}
            if said:  # nothing from a broken header should have been spoken
                said.clear()
    if not fields and raw.strip():  # the brain ignored the format: treat what it wrote as the reply...
        action = "chat"
        reply = _clean_reply(re.sub(r"^\s*[A-Z_]{3,12}\s*:\s*", "", raw))
        echo = difflib.SequenceMatcher(None, reply.lower(), text.lower()).ratio() > 0.8
        if reply and not echo:
            said.append(reply)
            await say(reply, to, store=False)
        # ...unless it only echoed the question back: then the normal chat brain answers (act -> chat())
    action = ALIAS_ACTIONS.get(action, action)
    if action not in ACTIONS:
        action = "chat"
    target = fields.get("TARGET", "").strip(" .")
    if action in ("open_app", "close_window", "minimize_window", "maximize_window", "switch_to_window") and not target:
        # the brain knew what to do but not on what: take the app / window named in what Hari said
        target = await asyncio.to_thread(_target_in, text, action) or ""
    number = re.search(r"-?\d+", fields.get("NUMBER") or "")
    d = {"action": action, "target": target,
         "seconds": int(number.group()) if number else 0, "count": int(number.group()) if number else 1,
         "reply": " ".join(said), "spoken": bool(said)}
    log.info("streamed turn %r → %s in %.1fs", text, {k: v for k, v in d.items() if v not in ("", 0, None)},
             time.time() - started)
    return d


async def warm_fillers() -> None:
    """Pre-render the fillers once, so they play instantly (voice.speak caches on disk)."""
    from . import engine

    while engine.starting:  # loading in its own thread (assistant.start)
        await asyncio.sleep(0.5)
    if CFG.get("local_engine") == "llamacpp" and engine.ready:
        # every model now runs in llama.cpp: free Ollama's copies (RAM is tight)
        for name in [CFG["ollama_model"]] + ([CFG["embed_model"]] if engine.embed_ready else []):
            try:
                await llm._ollama.post("/api/generate", json={"model": name, "keep_alive": 0})
            except Exception:  # noqa: BLE001
                pass
    await _fast_vectors()
    # Warm the brain's prompt cache for the voice persona + rules (~1,200 tokens, ~30 s on this CPU once),
    # so the first real conversation after a restart takes ~10 s, not ~40 s.
    try:
        async with llm.exclusive():
            await llm.complete(_command_system(), "VOICE CONVERSATION SO FAR:\n(none)\n\n---\nLATEST: hi",
                               schema=COMMAND_SCHEMA, max_tokens=8)
        log.info("assistant: brain warmed up")
    except Exception as e:  # noqa: BLE001
        log.info("warm-up skipped: %s", e)


# ---------------------------------------------------------------- doing it

def _norm_target(t: str) -> str:
    return re.sub(r"[^a-z ]", "", t.lower()).replace(" window", "").strip()


def _owner_ok(speaker: dict) -> bool:
    """PC control needs Hari's voice once it's enrolled (or a signed-in device without a voice)."""
    if not CFG["voice_owner_only"] or hearing.owner() is None:
        return True
    return speaker.get("role") == "owner" or speaker.get("device_trusted", False)


GUESSING = -0.6  # Whisper log-prob below this = it's guessing (Tamil on the English model, noise, distance)


async def act(d: dict, text: str, speaker: dict, to: str | None) -> None:
    a, target = d["action"], (d.get("target") or "").strip()
    if S.heard_conf < GUESSING and (a in PC_ACTIONS or a.startswith("close") or a.startswith("minimize")
                                     or S.heard_conf < -1.0):
        log.info("not acting on a guess (%.2f): %r -> %s", S.heard_conf, text, a)
        return await say(random.choice(["Sorry, I didn't catch that. Say it again?", "Sorry, didn't get that, once more?",
                                        "I didn't quite hear you. Come again?"]), to)
    S.quick = bool(d.get("fast"))  # instant commands: plain confirmations, no extra brain call (see natural())
    if d.get("spoken") and a in ("chat", "greet"):  # streamed: Dork already said it, sentence by sentence
        _store("assistant", d["reply"])
        _remember_said(d["reply"])
        return

    if a in PC_ACTIONS and not _owner_ok(speaker):
        hearing.log_auth("command", speaker, False, f"{a} {target}")
        await say(await natural(text, f"Refused: this voice isn't {memory.person()}'s, and only {memory.person()} "
                                      "can make you control the PC.", "Sorry, only Hari can ask me that."), to)
        return

    if a == "greet":
        return await say(_clean_reply(d.get("reply")) or await natural(text, "He's greeting you / checking you're there.",
                                                                       _greeting(speaker)), to)
    if a in ("yes", "no"):
        p, S.pending = S.pending, None
        if not p:
            return await chat(text, speaker, to)
        if a == "no":
            return await say(await natural(text, f"He said no to: {p['said']} — so you're leaving it as is.",
                                              random.choice(["Okay, leaving it.", "Alright, I'll leave it as is."])), to)
        if p["kind"] == "close_many":
            r = await asyncio.to_thread(desktop.window_action, "close", p["target"], True)
            return await say(await natural(text, r["said"]), to)
        return

    if a in ("open_app", "open_website", "open_folder", "switch_to_window", "maximize_window"):
        emit({"type": "ui", "do": "hide"})  # the always-on-top reactor would cover what was just opened
    if a == "open_app":
        # Already open? Bring it to the front instead of starting a second copy.
        if target and await asyncio.to_thread(desktop.find_windows, target) and \
                _norm_target(target) not in ("this", "that", "it"):
            r = await asyncio.to_thread(desktop.window_action, "focus", target)
            if r["ok"]:
                emit({"type": "ui", "do": "hide"})
                return await say(await natural(text, r["said"]), to)
        r = await asyncio.to_thread(desktop.open_app, target)
        if not r["ok"] and re.search(r"\.[a-z]{2,}$|youtube|google|github|gmail", target, re.I):
            r = desktop.open_url(target if "." in target else f"{target}.com")
        return await say(await natural(text, r["said"]), to)
    if a == "open_website":
        return await say(await natural(text, desktop.open_url(target or "https://www.google.com")["said"]), to)
    if a == "open_folder":
        return await say(await natural(text, (await asyncio.to_thread(desktop.open_folder, target or "home"))["said"]), to)
    if a in ("close_window", "minimize_window", "maximize_window", "switch_to_window"):
        kind = {"close_window": "close", "minimize_window": "minimize", "maximize_window": "maximize",
                "switch_to_window": "focus"}[a]
        r = await asyncio.to_thread(desktop.window_action, kind, target or "this")
        if kind == "focus" and not r["ok"] and target and _norm_target(target) not in ("this", "that", "it"):
            r = await asyncio.to_thread(desktop.open_app, target)  # "switch to Notepad" when it isn't open
        if r.get("confirm"):
            S.pending = {"kind": "close_many", "target": target, "said": r["said"]}
        if kind == "focus" and r["ok"]:
            emit({"type": "ui", "do": "hide"})
        return await say(await natural(text, r["said"]), to)
    if a == "media":
        r = await asyncio.to_thread(desktop.media, target or "play_pause", d.get("count") or 1)
        return await say(await natural(text, r["said"]), to)
    if a == "system_info":
        info = await asyncio.to_thread(desktop.system_info)
        fact = _readings(info, f"{target} {text}")
        return await say(await natural(text, f"The reading: {fact}", fact), to)
    if a == "look_up" and not await _really_lookup(text):
        return await chat(text, speaker, to, mode="off")  # small talk the brain mistook for a search
    if a == "look_up":  # the agent's own tool router double-checks that a lookup is really needed
        return await chat(text, speaker, to, mode="auto")
    if a == "set_timer":
        secs = int(d.get("seconds") or 0)
        if secs <= 0:
            return await say(await natural(text, "He wants a timer but didn't say how long — ask.",
                                                  random.choice(["For how long?", "Sure, how long should I set it for?"])), to)
        heard = set(re.findall(r"[a-z]+", text.lower()))
        if re.search(r"\b(second|minute|hour|min|sec)s?\b", target, re.I) or \
                not set(re.findall(r"[a-z]+", target.lower())) <= heard:
            target = ""  # the label must be words Hari actually said, not the duration or a guess
        fact = start_timer(secs, target)
        return await say(await natural(text, fact, fact), to)
    if a in ("open_chat", "open_settings", "show_memories"):
        emit({"type": "ui", "do": a, "conv": voice_conversation()["id"]})
        what = {"open_chat": "the chat window", "open_settings": "Settings", "show_memories": "your memory list"}[a]
        return await say(await natural(text, f"You opened {what} on screen for him.", f"Here's {what}."), to)
    if a == "go_to_sleep":
        await say(await natural(text, "He's done for now; you're going quiet until he calls you again.",
                                random.choice(["Alright, I'll be around.", "Sure. Call me when you need me.",
                                               "Okay, going quiet.", "Anytime. I'm here if you need me."])), to)
        S.awake_until = time.time() + 2.5
        return
    if a == "learn_voice":
        me = {memory.person().lower(), (speaker.get("name") or "").lower(), "me", "my", "mine", "my voice"}
        return await start_enrollment(None if not target or target.lower() in me else target, to)
    if a == "forget_voice":
        name = target or memory.person()
        hearing.forget(name)
        return await say(await natural(text, f"You deleted {name}'s stored voice.", f"Done, I've forgotten {name}'s voice."), to)
    if a == "who_is_speaking":
        if speaker.get("name"):
            fact = f"The voice matches {speaker['name']} ({round(speaker['score'] * 100)}% sure)."
        elif hearing.owner() is None:
            fact = "You don't know anyone's voice yet; they can say 'Dork, learn my voice'."
        else:
            fact = "You don't recognise this voice."
        return await say(await natural(text, fact, fact), to)
    if a == "lock":
        S.locked = True
        await say(await natural(text, "Guard mode is on: you'll ask for a voice check before waking again.",
                                "Guard mode on. I'll check your voice before I wake up again."), to)
        S.awake_until = time.time() + 3
        return
    if reply := _clean_reply(d.get("reply")):  # the command step already wrote the answer: one brain call
        return await say(reply, to)
    await chat(text, speaker, to, mode="off")  # plain conversation: no lookup step (faster)


GENERIC = re.compile(r"\s*((how|what) (else )?can i (help|assist|do)( you)?( for you)?( today| with)?[^.?!]*\??|"
                     r"is there anything else[^.?!]*[.?!]?)\s*$", re.I)


NATURAL = """You are Dork, Hari's personal assistant, talking out loud on a voice call. Write the ONE thing you say
next, given what Hari said and what just happened. One short, natural spoken sentence (two at most) — like a
friend, not a script: vary your wording, react to the situation, use the facts given. Reply in the language Hari
used (English, Tamil or a mix). Never customer-service lines, never "How can I help", no markdown or emojis.
If something failed, say so plainly and, if useful, what he could try. Write only the words to say."""


async def natural(heard: str, happened: str, fallback: str = "") -> str:
    """The spoken line for an event, in Dork's own words (fast cloud brain). Offline / slow → the plain fact."""
    if CFG.get("voice_brain") != "gemini" or not CFG.get("voice_natural") or llm.cloud_cooling() or S.quick:
        return fallback or happened
    convo = "\n".join(f"{'HARI' if m['role'] == 'user' else 'DORK'}: {m['content'][:200]}" for m in _window()[-6:])
    prompt = (f"Recent conversation:\n{convo or '(none)'}\n\nNow: {datetime.datetime.now():%A %H:%M}\n"
              f"Hari just said: {heard or '(nothing — he only called your name)'}\nWhat happened: {happened}")
    try:
        with llm.cloud_brain():
            if not llm.using_cloud():
                return fallback or happened
            line = await asyncio.wait_for(llm.complete(NATURAL, prompt, max_tokens=120), timeout=3)
        line = _clean_reply(line.strip().strip('"'))
        return line or fallback or happened
    except Exception as e:  # noqa: BLE001
        log.info("natural line failed (%s); saying the plain fact", e.__class__.__name__)
        return fallback or happened


def _clean_reply(r) -> str:
    r = re.sub(r"[*_#`]", "", str(r or "")).strip().strip('"').strip()
    return GENERIC.sub("", r).strip()


def _readings(info: dict, asked: str) -> str:
    """Live PC readings → one spoken line, without a trip through the brain (instant)."""
    asked = asked.lower()
    if re.search(r"\b(time|date|day|today)\b", asked) and not re.search(r"batter|cpu|ram|memory", asked):
        now = datetime.datetime.now()
        return f"It's {now:%I:%M %p}".replace(" 0", " ").lstrip("0") + f", {now:%A %d %B}."
    parts = []
    if "battery_percent" in info and (re.search(r"batter|charg|power|plug", asked) or not re.search(r"cpu|ram|memory|open|running", asked)):
        parts.append(f"Battery's at {info['battery_percent']} percent" + (", plugged in" if info.get("plugged_in") else ", on battery"))
    if re.search(r"cpu|processor|load|slow|memory|ram|system|performance", asked) or not parts:
        parts.append(f"CPU at {round(info['cpu_percent'])} percent, memory at {round(info['ram_percent'])}")
    if re.search(r"open|running|windows|apps", asked) and info.get("open_windows"):
        parts.append("open right now: " + ", ".join(info["open_windows"][:6]))
    return ". ".join(parts) + "."


async def chat(text: str, speaker: dict, to: str | None, extra: str = "", mode: str = "auto") -> None:
    """A spoken conversation turn through Dork's normal brain (memories, profile, web tools)."""
    conv = voice_conversation()
    memories = await memory.recall(text)
    system, history, note, _, _ = context.build(conv, memories)
    who = speaker.get("name")
    if who and speaker.get("role") != "owner":
        note += f"\n\nThe person speaking now is {who} (not {memory.person()}). Talk to them, not about them."
    elif not who and hearing.owner() is not None and speaker.get("source") == "ears":
        note += "\n\nThis voice isn't one you recognise: someone other than your owner may be talking."
    note += ("\n\nYou are in ASSISTANT MODE: Hari called you by voice and sees only your glowing reactor. "
             "Answer in one to three short spoken sentences.")
    if extra:
        note += "\n\n" + extra
    set_state("thinking")
    buf, full = "", []
    llm.chat_waiting += 1
    waiting = True
    try:
        with llm.cloud_brain():  # Gemini when configured, local brain as the fallback
            async with llm.exclusive():
                llm.chat_waiting -= 1
                waiting = False
                async for kind, value in agent.run(system, history, note, mode, voice=True):
                    if kind == "token":
                        buf += value
                        full.append(value)
                        # Speak sentence by sentence as the brain writes: Dork starts talking sooner.
                        while (m := re.search(r"(.+?[.!?])(\s+|$)", buf)) and len(m.group(1)) > 25:
                            emit({"type": "say", "text": m.group(1).strip(), "part": True}, to=to or "host")
                            _remember_said(m.group(1))
                            buf = buf[m.end():]
                    elif kind == "status":
                        emit({"type": "status", "text": value}, to=to or "host")
    except Exception as e:  # noqa: BLE001
        log.exception("assistant chat failed")
        buf += " Something went wrong on my side." if not full else ""
        if not full:
            full.append(f"Sorry, my brain hiccuped ({e.__class__.__name__}).")
            buf = full[0]
    finally:
        if waiting:
            llm.chat_waiting -= 1
    if buf.strip():
        emit({"type": "say", "text": buf.strip(), "part": True}, to=to or "host")
        _remember_said(buf)
    reply = "".join(full).strip()
    if reply:
        _store("assistant", reply)
        if not to and not hosts():
            await _play_locally(reply)
    emit({"type": "reply", "text": reply}, to=to or "host")
    set_state("awake")


# ---------------------------------------------------------------- timers

def start_timer(secs: int, label: str) -> str:
    tid = secrets.token_hex(3)
    due = time.time() + secs

    async def ring():
        await asyncio.sleep(secs)
        S.timers.pop(tid, None)
        emit({"type": "ui", "do": "show"})
        set_state("awake")
        fact = f"The {secs // 60 or secs} {'minute' if secs >= 60 else 'second'} timer he set is up" + (f" — it was for: {label}." if label else ".")
        await say(await natural("", fact, f"{memory.person()}, time's up" + (f": {label}." if label else ".")))

    S.timers[tid] = asyncio.get_running_loop().create_task(ring())
    mins = secs // 60
    span = f"{mins} minute{'s' if mins != 1 else ''}" if secs >= 60 and secs % 60 == 0 else f"{secs} seconds"
    log.info("timer %s in %ss (%s) due %s", tid, secs, label, datetime.datetime.fromtimestamp(due))
    return f"Timer set for {span}" + (f", for {label}." if label else ".")


# ---------------------------------------------------------------- enrollment by voice

ENROLL_LINES = [
    "Say: Dork, wake up, it's me.",
    "Now tell me what you did today, a sentence or two.",
    "Say: open Chrome and play some music.",
    "Count from one to ten.",
    "Tell me your favourite food and why.",
    "Last one: Dork, this is my voice, remember it.",
]


async def start_enrollment(name: str | None, to: str | None) -> None:
    owner = hearing.owner()
    person = name or (owner["name"] if owner else memory.person())
    if person in ("the user", ""):
        person = "Hari"
    role = "owner" if (not name or (owner and owner["name"] == name) or not owner) else "guest"
    S.enrolling = {"name": person, "role": role, "clips": [], "step": 0, "to": to}
    who = "your" if role == "owner" else f"{person}'s"
    await say(f"Okay, learning {who} voice. Six short phrases, in a normal voice. {ENROLL_LINES[0]}", to, store=False)
    emit({"type": "enroll", "step": 0, "of": len(ENROLL_LINES), "name": person})


async def enroll_step(samples: np.ndarray) -> None:
    e = S.enrolling
    if len(samples) < hearing.RATE * 0.8:
        return await say("A bit longer, please.", e["to"], store=False)
    e["clips"].append(samples)
    e["step"] += 1
    emit({"type": "enroll", "step": e["step"], "of": len(ENROLL_LINES), "name": e["name"]})
    if e["step"] < len(ENROLL_LINES):
        return await say(ENROLL_LINES[e["step"]], e["to"], store=False)
    S.enrolling = None
    try:
        r = await asyncio.to_thread(hearing.enroll, e["name"], e["clips"], e["role"])
    except ValueError as err:
        return await say(str(err), e["to"], store=False)
    hearing.log_auth("enroll", {"name": e["name"], "score": r["consistency"]}, True, f"{r['samples']} samples")
    quality = "Clear as a bell." if r["consistency"] > 0.75 else "Got it, though a few more samples later would help."
    await say(f"Done. I know {'your' if e['role'] == 'owner' else e['name'] + chr(39) + 's'} voice now. {quality}",
              e["to"], store=False)


# ---------------------------------------------------------------- guard mode (voice check with a random phrase)

WORDS = ["amber", "falcon", "river", "copper", "seven", "orbit", "maple", "thunder", "silver", "nine", "harbor",
         "lantern", "cobalt", "twelve", "meadow", "rocket"]


def _new_challenge() -> str:
    return " ".join(random.sample(WORDS, 3))


def _passes_challenge(text: str, speaker: dict) -> bool:
    said = set(re.findall(r"[a-z]+", text.lower().replace("12", "twelve").replace("7", "seven").replace("9", "nine")))
    words_ok = S.challenge and all(w in said for w in S.challenge.split())
    voice_ok = speaker.get("role") == "owner" and speaker.get("score", 0) >= CFG["voice_match_threshold"] + 0.05
    return bool(words_ok and voice_ok)


# ---------------------------------------------------------------- a phrase arrives

_busy = asyncio.Lock()


FOLLOW_UP = 25  # seconds the PC mic keeps listening for a follow-up without hearing "Dork"


def _sounds_like_media(text: str) -> bool:
    """Song lyrics / TV / Whisper hallucinations: long, or the same phrase over and over."""
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) > 45:
        return True
    phrases = [p.strip() for p in re.split(r"[.!?,]", text.lower()) if len(p.strip()) > 3]
    if len(phrases) >= 3 and max(phrases.count(p) for p in phrases) >= 3:
        return True
    # Whisper on noise loops on itself: "the end of the video is not the end of the video, but the end of…"
    tri = [tuple(words[i:i + 3]) for i in range(len(words) - 2)]
    return len(tri) >= 8 and len(set(tri)) / len(tri) < 0.6


def _remember_said(text: str) -> None:
    now = time.time()
    S.said = [x for x in S.said if now - x[0] < 90] + [(now, text)]
    S.spoke = [x for x in S.spoke if now - x[1] < 90]


def _overlaps_dork(span: tuple | None) -> bool:
    """Did this phrase happen (even partly) while Dork's voice was playing?"""
    if not span:
        return time.time() < S.speaking_until
    a, b = span
    return any(a < end and b > start for start, end in S.spoke)


def _echo_of_dork(text: str) -> bool:
    """Is this transcript just Dork's own recent words coming back through the mic?"""
    import difflib

    t = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
    if not t:
        return False
    for _, said in S.said:
        d = re.sub(r"[^a-z0-9 ]", "", voice.speakable(said).lower())
        m = difflib.SequenceMatcher(None, t, d)
        if t in d or m.ratio() > 0.6 or (len(t) > 12 and m.find_longest_match(0, len(t), 0, len(d)).size > 0.6 * len(t)):
            return True
    return False


async def handle(samples: np.ndarray, source: str = "ears", to: str | None = None, device_trusted: bool = False,
                 text: str | None = None, span: tuple | None = None) -> dict:
    """One spoken phrase → maybe wake, maybe act. Returns what happened (for the HTTP caller)."""
    async with _busy:
        # Voiceprint and offline transcription run side by side (different engines, both release the GIL).
        # Awake with the assistant window's own (echo-cancelled) mic running: that mic carries the conversation;
        # the background mic would only hear the same words again, plus Dork's echo.
        if source == "ears" and S.state != "sleeping" and time.time() < S.host_mic_until:
            return {"ignored": "window mic has the conversation"}
        # The PC mic while asleep is the gate for "is this Hari?": it gets the thorough overlap scan, and its
        # transcription waits for the verdict (no point transcribing a TV). Everything else runs in parallel.
        gate = source == "ears" and S.state == "sleeping"
        heard_task = None
        if text is None and len(samples) and not gate:
            heard_task = asyncio.ensure_future(asyncio.to_thread(hearing.transcribe_scored, samples))
        speaker = await asyncio.to_thread(hearing.identify, samples, gate) if len(samples) else {"name": None, "score": 0}
        speaker["source"] = source
        speaker["device_trusted"] = device_trusted  # a signed-in device (paired with a code / master key) is Hari
        S.last_speaker = speaker
        owner_enrolled = hearing.owner() is not None

        if S.enrolling:
            await enroll_step(samples)
            return {"heard": "", "speaker": speaker, "enrolling": True}

        # Dork's own voice coming back through the mic: by voiceprint, by timing, and by words (below).
        if speaker.get("role") == "self":
            return {"ignored": "dork's own voice", "speaker": speaker}
        if source == "ears" and _overlaps_dork(span):
            if not (owner_enrolled and speaker.get("role") == "owner"):
                return {"ignored": "dork speaking"}
            emit({"type": "stop_speaking"})  # Hari talking over Dork: barge in

        # Once Hari's voice is enrolled, other voices can't wake Dork from the background mic.
        if source == "ears" and S.state == "sleeping" and owner_enrolled and CFG["voice_owner_only"] \
                and speaker.get("role") != "owner":
            log.info("ignored a voice that isn't Hari's (best match %s, %.2f; needs %.2f)",
                     speaker.get("name") or "nobody", speaker.get("score", 0), CFG["voice_match_threshold"])
            return {"ignored": "not the owner", "speaker": speaker}

        if text is None:
            # Awake (or a phone/browser talking to Dork): Gemini hears requests far better than the small
            # offline model. Asleep, the free offline model only has to catch "Dork…".
            # Offline first (under a second). If it's sure, use it; if it's guessing (Tamil, Tanglish, distance,
            # noise) and Dork is in a conversation, ask Gemini, which hears those far better.
            text, conf = await heard_task if heard_task else await asyncio.to_thread(hearing.transcribe_scored, samples)
            in_conversation = source != "ears" or S.state != "sleeping"
            if in_conversation and CFG["voice_stt"] == "gemini" and conf < CFG["stt_trust"]:
                better = await hearing.transcribe_cloud(samples)
                if better is not None:
                    log.info("offline heard %r (%.2f) → gemini heard %r", text, conf, better)
                    text, conf = better, 0.0  # Gemini's hearing: trusted
            S.heard_conf = conf
        else:
            S.heard_conf = 0.0  # typed
        if not text:
            return {"heard": "", "speaker": speaker}
        if source == "ears" and _sounds_like_media(text):
            log.info("ignored (song / video / hallucination): %r", text[:80])
            return {"ignored": "media", "heard": text}
        if source == "ears" and _echo_of_dork(text):
            log.info("ignored echo of Dork's own words: %r", text)
            return {"ignored": "echo", "heard": text}
        llm.voice_until = time.time() + 90  # keep the brain free for the conversation
        log.info("heard %r from %s (%.2f) [%s]", text, speaker.get("name") or "unknown", speaker.get("score", 0), S.state)

        # The PC mic only takes commands right after a voice exchange; otherwise it needs "Dork, ..." first,
        # so a TV, a song or people chatting nearby can't keep Dork talking.
        # Awake (the reactor is up) = in conversation: everything said counts, no "Dork" needed. Asleep = needs the name.
        if source == "ears" and S.state == "sleeping":
            if S.locked and S.challenge:
                if _passes_challenge(text, speaker):
                    S.locked, S.challenge = False, None
                    hearing.log_auth("unlock", speaker, True)
                    bring_up()
                    set_state("awake")
                    await say(f"Voice confirmed. Welcome back, {memory.person()}.", to, store=False)
                else:
                    hearing.log_auth("unlock", speaker, False, text)
                    S.challenge = None
                    await say("That didn't check out.", to, store=False)
                return {"heard": text, "speaker": speaker}
            if not await is_wake(text):
                return {"heard": text, "speaker": speaker, "woke": False}
            if S.locked:
                S.challenge = _new_challenge()
                bring_up()
                emit({"type": "state", "state": "sleeping", "locked": True})
                await say(f"Voice check. Repeat after me: {S.challenge}.", to, store=False)
                return {"heard": text, "speaker": speaker, "challenge": True}
            hearing.log_auth("wake", speaker, True, text)
            S.armed_until = time.time() + FOLLOW_UP
            bring_up()
            emit({"type": "heard", "text": text, "speaker": speaker.get("name")})
            set_state("awake")
            # "Dork, open Chrome" wakes AND does it; a bare "Dork wake up" just gets a greeting.
            S.armed_until = time.time() + 600  # stay open while Dork works out the answer
            # Just the name ("Dork", "hey Dork?", "Dork, you there") is a hello, never a command.
            bare = _only_calling(text)
            d = {"action": "greet"} if bare else await understand(text, speaker, to)
            if d.get("spoken"):
                _store("user", text)
                await act(d, text, speaker, to)
            elif d["action"] in ("greet", "who_is_speaking", "go_to_sleep") or (d["action"] == "chat" and len(text.split()) <= 4):
                await say(await natural(text, "He just called your name to get your attention; nothing asked yet. "
                                              "Greet him back naturally and briefly.", _greeting(speaker)), to)
            else:
                _store("user", text)
                await act(d, text, speaker, to)
            S.armed_until = time.time() + FOLLOW_UP  # extended again when the greeting / answer finishes
            return {"heard": text, "speaker": speaker, "woke": True}

        if S.state == "sleeping":  # a phone/browser talking to Dork directly
            set_state("awake")
        emit({"type": "heard", "text": text, "speaker": speaker.get("name")}, to=to or None)
        set_state("thinking")
        if source == "ears":
            S.armed_until = time.time() + FOLLOW_UP
        _store("user", text, "voice" if source == "ears" else "voice (device)")
        if source == "ears":
            S.armed_until = time.time() + 600  # stay open while Dork works out the answer
        d = await understand(text, speaker, to)
        await act(d, text, speaker, to)
        if source == "ears" and d["action"] != "go_to_sleep":
            S.armed_until = time.time() + FOLLOW_UP  # extended again when Dork's voice finishes (spoken())
        if S.state == "thinking":
            set_state("awake")
        return {"heard": text, "speaker": speaker, "action": d["action"]}


_last_launch = 0.0


def bring_up() -> None:
    """Show the reactor. If no Dork window is open at all (the app was quit), start the desktop app
    straight into assistant mode — waking Dork never opens the text chat."""
    global _last_launch
    emit({"type": "ui", "do": "show"})
    if hosts() or time.time() - _last_launch < 20:
        return
    import os
    import subprocess

    from . import oskit
    exe = oskit.desktop_app()
    if exe:
        _last_launch = time.time()
        env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
        subprocess.Popen([str(exe), "--assistant"], env=env, **oskit.detached())
        log.info("assistant: no window open — started Dork Desktop in assistant mode")


def _greeting(speaker: dict) -> str:
    name = speaker.get("name") if speaker.get("role") == "owner" else None
    name = name or memory.person()
    h = datetime.datetime.now().hour
    lines = [f"Right here, {name}.", "Online. What do you need?", f"At your service, {name}.", "Listening.",
             "I'm up. Go ahead."]
    if h < 5 or h >= 23:
        lines.append(f"Burning the midnight oil, {name}?")
    return random.choice(lines)


# ---------------------------------------------------------------- the ears: background microphone loop

_stop = threading.Event()


def _ears_thread() -> None:
    import sounddevice as sd

    det = hearing.vad()
    chunks: queue.Queue = queue.Queue(maxsize=200)
    try:
        dev, index = sd.query_devices(kind="input"), None
        if CFG["ears_device"]:  # a chosen mic (e.g. a headset), matched by name; else the Windows default
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0 and CFG["ears_device"].lower() in d["name"].lower():
                    dev, index = d, i
                    break
        rate = hearing.RATE
        try:
            sd.check_input_settings(device=index, samplerate=rate, channels=1)
        except Exception:  # noqa: BLE001 — record at the mic's rate and resample
            rate = int(dev["default_samplerate"])
        stream = sd.InputStream(device=index, samplerate=rate, channels=1, dtype="float32", blocksize=int(rate * 0.032),
                                callback=lambda data, *_: chunks.put_nowait(data[:, 0].copy()) if not chunks.full() else None)
        stream.start()
    except Exception as e:  # noqa: BLE001
        S.ears_ok, S.ears_error = False, f"{e.__class__.__name__}: {e}"
        log.warning("ears: microphone unavailable: %s", e)
        return
    S.ears_ok, S.ears_error = True, ""
    log.info("ears: listening on %s at %d Hz", dev["name"], rate)
    last_level = last_fg = 0.0
    try:
        while not _stop.is_set():
            try:
                data = chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            if rate != hearing.RATE:
                n = int(len(data) * hearing.RATE / rate)
                data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data).astype(np.float32)
            if CFG["ears_gain"] != 1:  # quiet laptop mics: lift the signal so far-off speech still counts
                data = np.clip(data * CFG["ears_gain"], -1.0, 1.0).astype(np.float32)
            det.accept_waveform(data)
            now = time.time()
            if now - last_level > 0.07 and S.state != "sleeping" and _clients:
                emit({"type": "level", "v": round(hearing.level(data), 3), "speech": det.is_speech_detected()})
                last_level = now
            if now - last_fg > 0.5:
                desktop.track_foreground()
                last_fg = now
                if S.state == "awake" and now > S.awake_until and now > S.speaking_until and not S.enrolling:
                    S.state = "sleeping"
                    emit({"type": "state", "state": "sleeping", "locked": S.locked})
                    emit({"type": "ui", "do": "hide"})
            while not det.empty():
                seg = np.array(det.front.samples, dtype=np.float32)
                det.pop()
                end = time.time() - 0.5  # the detector closes a phrase after ~0.55 s of silence
                span = (end - len(seg) / hearing.RATE - 0.3, end)
                if _loop:
                    asyncio.run_coroutine_threadsafe(handle(seg, "ears", span=span), _loop)
    finally:
        stream.stop()
        stream.close()


def start(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop
    from . import engine

    if CFG.get("local_engine") == "llamacpp":
        engine.starting = True  # set now, so jobs queued at startup wait for the C++ brain
        threading.Thread(target=engine.start, daemon=True, name="engine").start()
    threading.Thread(target=hearing.warm, daemon=True, name="hearing-warm").start()
    loop.create_task(hearing.learn_own_voice())
    loop.create_task(warm_fillers())
    if CFG["ears"]:
        threading.Thread(target=_ears_thread, daemon=True, name="ears").start()


def stop() -> None:
    _stop.set()
    for t in S.timers.values():
        t.cancel()


async def wake_from_ui(to: str | None = None) -> None:
    """Woken by the hotkey / a click on the reactor rather than by voice."""
    if S.locked:
        S.challenge = _new_challenge()
        await say(f"Voice check. Repeat after me: {S.challenge}.", to, store=False)
        return
    set_state("awake")
