"""Long-term memory and the user profile.

Profile  — the user's details: what they typed (name, what to call them, about) plus a
           short summary Dork writes from its memories ("gathered"). Uploading a file
           (resume, bio, notes) feeds memories, which feed the summary.
Memories — durable facts Dork learns across ALL chats. Extraction runs on batches of
           conversation (not every line), keeps only important facts, merges
           near-duplicates, and recall injects a fixed small number per message —
           so the context cost stays flat no matter how many memories pile up.
"""

import asyncio
import json
import logging
import math
import re

from . import db, llm
from .config import CFG

log = logging.getLogger("dork.memory")

CATEGORIES = ("personal", "professional", "passion", "preference", "goal", "other")

# ---------------------------------------------------------------- the memory tree
# Every memory lives at a path like "People/Family/Sister (Priya)" or "Interests/Games/Valorant".
# The roots are fixed; everything below them is created as topics come up.
ROOTS = {
    "You": "who the user is: identity, background, skills, health, personality",
    "People": "people in the user's life, one branch per person: People/Family/Sister (Priya), People/Friends/Arun",
    "Interests": "passions, hobbies, games, sports, music, shows — one branch each: Interests/Games/Valorant",
    "Work & Studies": "jobs, college, courses, projects — one branch per project: Work & Studies/Projects/Robotic arm",
    "Goals": "plans and ambitions",
    "Preferences": "how the user likes things done, likes and dislikes",
    "Life": "places, routines, important dates, belongings, anything else",
}
ROOT_CATEGORY = {"You": "personal", "People": "personal", "Interests": "passion", "Work & Studies": "professional",
                 "Goals": "goal", "Preferences": "preference", "Life": "other"}
CATEGORY_ROOT = {"personal": "You", "professional": "Work & Studies", "passion": "Interests",
                 "preference": "Preferences", "goal": "Goals", "other": "Life"}
ROOT_ALIASES = {
    **{w: "Interests" for w in ("games", "gaming", "hobbies", "hobby", "passion", "passions", "sports", "music",
                                "movies", "shows", "books", "reading", "interest")},
    **{w: "People" for w in ("family", "friends", "friend", "relatives", "colleagues", "person")},
    **{w: "Work & Studies" for w in ("work", "studies", "work and studies", "education", "projects", "project",
                                     "career", "college", "job", "school")},
    **{w: "You" for w in ("me", "self", "identity", "personal", "health", "skills")},
    **{w: "Goals" for w in ("goal", "plans", "ambitions", "dreams")},
    **{w: "Preferences" for w in ("preference", "likes", "dislikes")},
}
ROOT_SYNONYMS = {"work", "studies", "work and studies", "me", "self", "identity", "personal", "goal", "preference",
                 "interest", "person", "passion", "passions"}
ABOUT_DORK = re.compile(r"^\s*(dork|the assistant|the ai)\b|\b(dork|the assistant) (explained|said|stated|replied|told|"
                        r"provided|mentioned|offered|suggested|asked|clarified)\b|text-based ai|"
                        r"\b(requested|asked|wanted) (dork|the assistant) to (check|look|search|open|find|browse|show|see)\b", re.I)
TREE_GUIDE ="\n".join(f"- {k}: {v}" for k, v in ROOTS.items())


def clean_path(path: str, fallback_category: str = "other") -> str:
    """Normalise a path: known root first, max 4 levels, tidy names."""
    parts = [p.strip(" .-_") for p in re.split(r"\s*(?:/|>|›|»|\\)\s*", str(path or "")) if p.strip(" .-_")]
    parts = [p[:1].upper() + p[1:] for p in parts][:4]
    roots_lc = {r.lower(): r for r in ROOTS}
    if parts and parts[0].lower() in roots_lc:
        parts[0] = roots_lc[parts[0].lower()]
    elif parts and parts[0].lower() in ROOT_ALIASES:
        parts.insert(0, ROOT_ALIASES[parts[0].lower()])  # "Games/Valorant" → "Interests/Games/Valorant"
        if parts[1].lower() in ROOT_SYNONYMS:            # "Work/Projects" → "Work & Studies/Projects"
            parts.pop(1)
    else:
        parts.insert(0, CATEGORY_ROOT.get(fallback_category, "Life"))
    # The user isn't one of "People": People/<their name>/… → You/…
    prof = get_profile()
    me = {n.lower() for n in (prof["name"], prof["call_me"]) if n} | {"me", "user", "myself"}
    if parts[0] == "People" and len(parts) > 1 and parts[1].lower() in me:
        parts = ["You"] + parts[2:]
    # A root repeated deeper down ("You/Preferences/…") means that root: restart from the last one.
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] in ROOTS:
            parts = parts[i:]
            break
    return "/".join(p[:40] for p in parts[:4])


def tree_paths(limit: int = 60) -> list[str]:
    """Existing branches (most used first) so new memories reuse them instead of duplicating."""
    rows = db.q("SELECT path, COUNT(*) AS n FROM memories WHERE path IS NOT NULL GROUP BY path ORDER BY n DESC LIMIT ?", (limit,))
    return [r["path"] for r in rows]
DUPLICATE_SIMILARITY = 0.88     # embeddings closer than this = same memory, update instead of insert
CONTEXT_MESSAGES = 4            # already-processed messages shown to the extractor for context
MAX_BATCH_MESSAGES = 30

# ---------------------------------------------------------------- profile

PROFILE_TEXT = ("name", "call_me", "about", "gathered")


def get_profile() -> dict:
    stored = json.loads(db.get_meta("profile") or "{}")
    profile = {f: str(stored.get(f, "")).strip() for f in PROFILE_TEXT}
    profile["gathered_at"] = stored.get("gathered_at", 0)
    profile["learn_from_chats"] = bool(stored.get("learn_from_chats", True))
    return profile


def set_profile(**fields) -> dict:
    profile = get_profile()
    for k, v in fields.items():
        if k in PROFILE_TEXT:
            profile[k] = str(v).strip()[:3000]
        elif k == "learn_from_chats":
            profile[k] = bool(v)
        elif k == "gathered_at":
            profile[k] = v
    db.set_meta("profile", json.dumps(profile))
    return profile


GATHER_PROMPT = """You keep a short profile of the person a personal AI assistant works for, written from notes the assistant has gathered.
Write 3-8 short bullet points (max 150 words): who they are, what they do, what they care about, and how they like to be helped.
Use only the notes. Don't invent, don't repeat what's already in WHAT THEY WROTE, no preamble — just the bullets.
Only facts about the PERSON — never about the assistant, what it can or can't do, or what it said.
Don't mention a nickname or preferred name unless a note states one explicitly.
If the notes say nothing real about the person, reply with nothing."""


async def refresh_gathered() -> dict:
    """Rewrite the 'what Dork has learned' summary from the most important memories."""
    notes = db.q("SELECT content FROM memories ORDER BY importance DESC, updated_at DESC LIMIT 80")
    if not notes:
        return set_profile(gathered="", gathered_at=db.now())
    p = get_profile()
    wrote = f"name: {p['name'] or '?'}; call them: {p['call_me'] or '?'}; about: {p['about'] or '?'}"
    summary = await llm.complete(
        GATHER_PROMPT,
        f"WHAT THEY WROTE: {wrote}\n\nNOTES:\n" + "\n".join(f"- {n['content']}" for n in notes),
        max_tokens=1500,
    )
    return set_profile(gathered=summary.strip(), gathered_at=db.now())


def person() -> str:
    """How Dork refers to its creator in prompts and memories."""
    p = get_profile()
    return p["call_me"] or p["name"] or "the user"


# ---------------------------------------------------------------- similarity

def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _keyword_score(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


# ---------------------------------------------------------------- store / recall

def list_all() -> list[dict]:
    return db.q("SELECT id, content, category, path, importance, hits, created_at, updated_at FROM memories ORDER BY path, updated_at DESC")


def move(memory_id: int, path: str) -> None:
    path = clean_path(path)
    db.x("UPDATE memories SET path=?, category=?, updated_at=? WHERE id=?",
         (path, ROOT_CATEGORY[path.split("/")[0]], db.now(), memory_id))


async def add(content: str, path: str = "", importance: int = 3, source: str = "auto", category: str = "other") -> dict:
    """Insert a memory, or update a near-duplicate instead of storing it twice."""
    content = content.strip()
    if ABOUT_DORK.search(content):
        # Notes about Dork's own replies ("Dork explained it can't…") aren't facts about the user,
        # and once recalled they make Dork repeat them. Never store them.
        log.info("skipped a memory about Dork itself: %s", content[:80])
        return {"id": None, "skipped": True}
    path = clean_path(path, category)
    category = ROOT_CATEGORY[path.split("/")[0]]
    importance = max(1, min(5, int(importance)))
    vecs = await llm.embed([content])
    vec = vecs[0] if vecs else None

    best, best_score = None, 0.0
    for m in db.q("SELECT id, content, importance, embedding FROM memories"):
        if vec and m["embedding"]:
            score = _cosine(vec, json.loads(m["embedding"]))
        else:
            score = _keyword_score(content, m["content"]) + 0.1  # rough fallback without embeddings
        if score > best_score:
            best, best_score = m, score

    if best and best_score >= DUPLICATE_SIMILARITY:
        db.x(
            "UPDATE memories SET content=?, category=?, path=?, importance=?, embedding=?, updated_at=? WHERE id=?",
            (content, category, path, max(importance, best["importance"]), json.dumps(vec) if vec else best["embedding"], db.now(), best["id"]),
        )
        return {"id": best["id"], "updated": True}

    mid = db.x(
        "INSERT INTO memories(content, category, path, importance, embedding, source, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (content, category, path, importance, json.dumps(vec) if vec else None, source, db.now(), db.now()),
    )
    return {"id": mid, "updated": False}


async def recall(query: str, k: int | None = None) -> list[dict]:
    """The few memories most relevant to this message (from every chat), plus core facts.
    Always a bounded number, however many memories exist."""
    k = k or CFG["memory_top_k"]
    rows = db.q("SELECT id, content, category, path, importance, embedding FROM memories")
    if not rows:
        return []
    vecs = await llm.embed([query])
    qvec = vecs[0] if vecs else None

    scored = []
    for m in rows:
        if qvec and m["embedding"]:
            rel = _cosine(qvec, json.loads(m["embedding"]))
        else:
            rel = _keyword_score(query, m["content"])
        scored.append((rel + 0.03 * m["importance"], m))
    scored.sort(key=lambda s: s[0], reverse=True)

    picked = [m for _, m in scored[:k]]
    # Core facts (importance 5) always ride along, up to 3 of them.
    for m in [m for m in sorted(rows, key=lambda r: -r["importance"]) if m["importance"] >= 5][:3]:
        if m not in picked:
            picked.append(m)

    if picked:
        ids = ",".join(str(m["id"]) for m in picked)
        db.x(f"UPDATE memories SET hits = hits + 1 WHERE id IN ({ids})")
    return [{"id": m["id"], "content": m["content"], "category": m["category"], "path": m["path"] or ""} for m in picked]


async def reindex() -> None:
    """Give every memory an embedding of the current model/size (after upgrades or outages)."""
    probe = await llm.embed(["probe"])
    if not probe:
        return
    dim = len(probe[0])
    # Vectors from different models aren't comparable even at the same size: re-embed all on a change.
    model_changed = db.get_meta("embed_model") != llm.EMBED_ID
    stale = [m for m in db.q("SELECT id, content, embedding FROM memories")
             if model_changed or not m["embedding"] or len(json.loads(m["embedding"])) != dim]
    for i in range(0, len(stale), 50):
        batch = stale[i:i + 50]
        vecs = await llm.embed([m["content"] for m in batch])
        if not vecs:
            return
        for m, v in zip(batch, vecs):
            db.x("UPDATE memories SET embedding=? WHERE id=?", (json.dumps(v), m["id"]))
    db.set_meta("embed_model", llm.EMBED_ID)
    if stale:
        log.info("re-indexed %d memories", len(stale))


# ---------------------------------------------------------------- extraction

EXTRACT_PROMPT = """You are the long-term memory of Dork, a personal AI assistant. Below is a recent stretch of conversation between {person} and Dork.
Decide what, if anything, is worth remembering for months. Think like a close friend quietly taking notes, not a stenographer.

REMEMBER: lasting facts about {person}'s life, people, work, projects, skills, goals, habits, strong preferences, passions, important dates, and anything they explicitly ask Dork to remember.
DO NOT REMEMBER: greetings, small talk, one-off questions or requests, passing moods, things only true today, general knowledge, Dork's own replies, things already in the PROFILE, or anything already in KNOWN MEMORIES (unless it changed — then write the updated version).
ONE memory per topic: never put different people, hobbies or projects in the same memory. Details about the same
topic belong together in one memory. Write each memory as one standalone sentence.
Importance: 5 = core identity, never forget; 4 = important; 3 = useful. If it's below 3, leave it out.
Most conversations contain 0-2 things worth remembering. An empty list is normal and correct.

FILE EACH MEMORY in the memory tree with a "path". Roots:
{tree}
Give each person, game, hobby, project etc. its own branch (e.g. "People/Family/Sister (Priya)",
"Interests/Games/Valorant", "Work & Studies/Projects/Robotic arm"). Reuse an EXISTING BRANCH when one fits.
A path is ONE branch, never a list.

EXAMPLE — "my brother Ravi got into IIT, and I've started learning guitar" gives TWO memories:
  {{"content": "{person}'s brother Ravi got into IIT.", "path": "People/Family/Brother (Ravi)", "importance": 4}}
  {{"content": "{person} has started learning guitar.", "path": "Interests/Music/Guitar", "importance": 3}}"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "path": {"type": "string"},
                    "importance": {"type": "integer"},
                },
                "required": ["content", "path", "importance"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}

_conv_locks: dict[str, asyncio.Lock] = {}


def unprocessed_user_messages(conversation_id: str) -> int:
    conv = db.one("SELECT memory_upto FROM conversations WHERE id=?", (conversation_id,))
    if not conv:
        return 0
    return db.one(
        "SELECT COUNT(*) AS n FROM messages WHERE conversation_id=? AND id>? AND role='user'",
        (conversation_id, conv["memory_upto"] or 0),
    )["n"]


async def extract_from_conversation(conversation_id: str) -> int:
    """Background job: read the not-yet-processed part of a chat (with a little earlier
    context) and store only what's worth keeping. Returns count stored."""
    lock = _conv_locks.setdefault(conversation_id, asyncio.Lock())
    async with lock:
        conv = db.one("SELECT memory_upto FROM conversations WHERE id=?", (conversation_id,))
        if not conv:
            return 0
        upto = conv["memory_upto"] or 0
        if not get_profile()["learn_from_chats"]:
            # Learning is switched off: mark everything as reviewed without reading it.
            db.x("UPDATE conversations SET memory_upto=(SELECT COALESCE(MAX(id), ?) FROM messages WHERE conversation_id=?) WHERE id=?",
                 (upto, conversation_id, conversation_id))
            return 0
        new = db.q(
            "SELECT id, role, content FROM messages WHERE conversation_id=? AND id>? ORDER BY id LIMIT ?",
            (conversation_id, upto, MAX_BATCH_MESSAGES),
        )
        if not any(m["role"] == "user" for m in new):
            return 0
        earlier = list(reversed(db.q(
            "SELECT role, content FROM messages WHERE conversation_id=? AND id<=? ORDER BY id DESC LIMIT ?",
            (conversation_id, upto, CONTEXT_MESSAGES),
        )))

        who = person()
        fmt = lambda ms: "\n".join(f"{who if m['role'] == 'user' else 'Dork'}: {m['content'][:1500]}" for m in ms)
        user_text = " ".join(m["content"] for m in new if m["role"] == "user")
        known = "\n".join(f"- {m['content']}" for m in await recall(user_text[:2000], k=10)) or "(none)"
        p = get_profile()
        profile = f"name: {p['name'] or '?'}; call them: {p['call_me'] or '?'}; about: {p['about'] or '?'}"

        raw = await llm.complete(
            EXTRACT_PROMPT.format(tree=_tree_hint(), person=who),
            f"PROFILE: {profile}\n\nKNOWN MEMORIES:\n{known}\n\n"
            + (f"EARLIER (context only, already processed):\n{fmt(earlier)}\n\n" if earlier else "")
            + f"NEW CONVERSATION TO REVIEW:\n{fmt(new)}",
            schema=EXTRACT_SCHEMA,
            max_tokens=2000,
        )
        try:
            items = json.loads(raw).get("memories", [])
        except (json.JSONDecodeError, AttributeError):
            log.warning("memory extraction returned non-JSON: %r", raw[:200])
            items = []

        stored = 0
        for item in items[:5]:
            if not isinstance(item, dict) or not str(item.get("content", "")).strip():
                continue
            try:
                importance = int(item.get("importance", 3))
            except (TypeError, ValueError):
                importance = 3
            if importance < CFG["memory_min_importance"]:
                continue
            await add(str(item["content"]), str(item.get("path", "")), importance, source=conversation_id)
            stored += 1

        db.x("UPDATE conversations SET memory_upto=? WHERE id=?", (new[-1]["id"], conversation_id))
        log.info("memory review of %s: %d new message(s), %d memory(ies) kept", conversation_id, len(new), stored)
        if stored:
            await refresh_gathered()
        return stored


def _tree_hint() -> str:
    existing = tree_paths()
    return TREE_GUIDE + ("\nEXISTING BRANCHES:\n" + "\n".join(f"- {p}" for p in existing) if existing else "")


DOC_PROMPT = """You are the long-term memory of Dork, a personal AI assistant. {person} uploaded a document about themselves (a resume, bio, notes, etc.).
Extract the durable facts worth remembering: identity, education, work, skills, projects, goals, interests, important people and dates.
Merge related details into one memory. Write each memory as one standalone sentence about {person}. Skip anything already in KNOWN MEMORIES.
Importance: 5 = core identity; 4 = important; 3 = useful. Leave out anything less. At most 15 memories.
File each memory in the memory tree with a "path". Roots:
{tree}
Give each person, project, hobby etc. its own branch. Reuse an EXISTING BRANCH when one fits."""

DOC_CHUNK_CHARS = 12000
DOC_MAX_CHUNKS = 5


async def learn_from_document(filename: str, text: str) -> int:
    """Turn an uploaded document into memories, then refresh the profile summary."""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    who = person()
    stored = 0
    for i in range(0, min(len(text), DOC_CHUNK_CHARS * DOC_MAX_CHUNKS), DOC_CHUNK_CHARS):
        chunk = text[i:i + DOC_CHUNK_CHARS]
        known = "\n".join(f"- {m['content']}" for m in await recall(chunk[:2000], k=15)) or "(none)"
        raw = await llm.complete(
            DOC_PROMPT.format(tree=_tree_hint(), person=who),
            f"KNOWN MEMORIES:\n{known}\n\nDOCUMENT ({filename}):\n{chunk}",
            schema=EXTRACT_SCHEMA,
            max_tokens=4000,
        )
        try:
            items = json.loads(raw).get("memories", [])
        except (json.JSONDecodeError, AttributeError):
            log.warning("document extraction returned non-JSON: %r", raw[:200])
            continue
        for item in items[:15]:
            if not isinstance(item, dict) or not str(item.get("content", "")).strip():
                continue
            try:
                importance = int(item.get("importance", 3))
            except (TypeError, ValueError):
                importance = 3
            if importance >= CFG["memory_min_importance"]:
                await add(str(item["content"]), str(item.get("path", "")), importance, source=f"upload:{filename}")
                stored += 1
    log.info("learned %d memories from %s", stored, filename)
    await refresh_gathered()
    return stored


# ---------------------------------------------------------------- filing older memories

CLASSIFY_SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}


async def file_unsorted() -> int:
    """Give memories saved before the tree existed a place in it (runs at startup)."""
    for m in db.q("SELECT id, path FROM memories WHERE path IS NOT NULL AND path != ''"):
        fixed = clean_path(m["path"])  # tidy anything filed oddly by an earlier run
        if fixed != m["path"]:
            db.x("UPDATE memories SET path=? WHERE id=?", (fixed, m["id"]))
    rows = db.q("SELECT id, content, category FROM memories WHERE path IS NULL OR path = ''")
    for m in rows:
        try:
            raw = await llm.complete(
                f"You file a personal assistant's memories about {person()} into a tree. Roots:\n" + _tree_hint()
                + f"\nFacts about {person()} themself go under You (or Interests, Work & Studies, Goals, Preferences, Life);"
                " People is only for OTHER people. Give each person, project, hobby, game etc. its own branch."
                " Reply with the path only.",
                f"MEMORY: {m['content']}", schema=CLASSIFY_SCHEMA, max_tokens=100)
            path = json.loads(raw).get("path", "")
        except Exception:  # noqa: BLE001 — fall back to the old flat category
            path = ""
        move(m["id"], clean_path(path, m["category"]))
    if rows:
        log.info("filed %d memories into the tree", len(rows))
    return len(rows)
