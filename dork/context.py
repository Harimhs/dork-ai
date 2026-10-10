"""Context window management: persona + memories + rolling summary + as much
recent history as fits. Older messages get folded into a summary."""

import datetime

from . import VERSION, db, llm, memory
from .config import CFG

SUMMARIZE_AFTER = 6  # summarize once this many messages have fallen out of the window


# Stable across turns (so it can be cached). Per-turn facts go in the note below.
PERSONA = """You are Dork, a personal AI assistant — think JARVIS, but real and personal. Version: {version}. You were built by {creator}.

THE PERSON YOU'RE TALKING TO
{user}

PERSONALITY
Loyal, sharp, warm, a little witty, never servile or over-apologetic. Talk like a trusted friend and right hand — never "Master", "sir" or "boss" unless they ask.
Use their name only now and then, the way a friend would. Most replies shouldn't include it.
Concise and direct by default; go deeper when asked or when it matters. Use markdown when it helps. Begin your visible answer immediately.
You remember things across all your conversations. Each turn you're given what you currently recall — use it naturally, don't recite it.
If you don't know something, say so plainly. Never invent memories.
Never sound like customer service: no "How can I assist you today?", no "Is there anything else I can help with?",
no listing what you can do. Greet like a friend who's been around all day, then get to the point.
Never describe yourself as "just a text-based AI" — you're Dork."""


def _user_block() -> str:
    p = memory.get_profile()
    lines = []
    if p["name"]:
        lines.append(f"Name: {p['name']}")
    if p["call_me"] and p["call_me"] != p["name"]:
        lines.append(f"Prefers to be called: {p['call_me']}")
    if p["about"]:
        lines.append(f"In their own words: {p['about']}")
    if p["gathered"]:
        lines.append(f"What you've learned about them:\n{p['gathered']}")
    if not lines:
        return "You don't know much about them yet. Early on, ask once — casually — what they'd like you to call them."
    return "\n".join(lines)


def est_tokens(text: str) -> int:
    # ~3.5 chars per token for English; slightly pessimistic on purpose.
    return len(text) // 3 + 4


def build(conversation: dict, memories: list[dict]):
    """Returns (system, history, note, stats, overflow).

    system   — stable persona (cacheable)
    history  — the most recent messages that fit the budget
    note     — per-turn context: time, recalled memories, earlier-conversation summary
    overflow — messages that fell out of the window and aren't summarized yet
    """
    system = PERSONA.format(version=VERSION, creator=CFG["creator_name"], user=_user_block())
    note = f"Current date/time: {datetime.datetime.now().strftime('%A, %d %B %Y, %H:%M')}."
    if memories:
        note += "\n\nMemories relevant to this message:\n" + "\n".join(
            f"- [{m['path'].replace('/', ' > ')}] {m['content']}" if m.get("path") else f"- {m['content']}" for m in memories)
    if conversation.get("summary"):
        note += "\n\nSummary of earlier parts of this conversation:\n" + conversation["summary"]

    fixed = est_tokens(system) + est_tokens(note)
    budget = llm.CONTEXT_LIMIT - llm.REPLY_RESERVE - fixed

    rows = db.q(
        "SELECT id, role, content FROM messages WHERE conversation_id=? AND id>? ORDER BY id DESC",
        (conversation["id"], conversation.get("summarized_upto") or 0),
    )
    kept, used, overflow = [], 0, []
    for i, m in enumerate(rows):
        cost = est_tokens(m["content"])
        if used + cost > budget and kept:
            overflow = list(reversed(rows[i:]))
            break
        kept.append(m)
        used += cost
    kept.reverse()
    # The API expects history to start with a user turn.
    while kept and kept[0]["role"] != "user":
        overflow.append(kept.pop(0))

    history = [{"role": m["role"], "content": m["content"]} for m in kept]
    stats = {
        "used": fixed + used,
        "limit": llm.CONTEXT_LIMIT,
        "reserved_for_reply": llm.REPLY_RESERVE,
        "messages_in_window": len(kept),
        "messages_summarized": db.one(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id=? AND id<=?",
            (conversation["id"], conversation.get("summarized_upto") or 0),
        )["n"],
        "memories_used": len(memories),
    }
    return system, history, note, stats, overflow


async def summarize_overflow(conversation_id: str, overflow: list[dict]) -> None:
    """Background job: fold messages that no longer fit into the rolling summary."""
    if len(overflow) < SUMMARIZE_AFTER:
        return
    conv = db.one("SELECT summary FROM conversations WHERE id=?", (conversation_id,))
    if conv is None:
        return
    who = memory.person()
    transcript = "\n".join(f"{who if m['role'] == 'user' else 'Dork'}: {m['content'][:1500]}" for m in overflow)
    summary = (
        await llm.complete(
            f"Update the running summary of a conversation between {who} and Dork. Keep decisions, facts, "
            "open tasks and context needed to continue. Max 200 words. Output only the summary.",
            f"CURRENT SUMMARY:\n{conv['summary'] or '(empty)'}\n\nNEW MESSAGES:\n{transcript}",
            max_tokens=2000,
        )
    ).strip()
    if summary:
        db.x(
            "UPDATE conversations SET summary=?, summarized_upto=? WHERE id=?",
            (summary, overflow[-1]["id"], conversation_id),
        )
