"""HTTP API + web UI. Every device talks to this; only this machine runs the model."""

import asyncio
import base64
import io
import json
import logging
import secrets
import time

import httpx

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import VERSION, agent, assistant, auth, context, db, greeting, hearing, llm, memory, voice
from .websearch import browser
from .config import CFG, DATA_DIR, WEB_DIR

log = logging.getLogger("dork")
app = FastAPI(title="Dork", version=VERSION, docs_url=None, redoc_url=None)
_background: set[asyncio.Task] = set()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def run_in_background(job, *args) -> None:
    """Queue a model job behind any chat reply (chat always goes first)."""

    async def runner():
        try:
            while llm.LOCAL and (llm.chat_waiting or time.time() < llm.voice_until):
                await asyncio.sleep(0.5)
            async with llm.exclusive():
                await job(*args)
        except Exception:
            log.exception("background job %s failed", getattr(job, "__name__", job))

    task = asyncio.create_task(runner())
    _background.add(task)
    task.add_done_callback(_background.discard)


_memory_timers: dict[str, asyncio.Task] = {}


def schedule_memory_review(conv_id: str) -> None:
    """Review a chat for memories in batches, not after every line: right away once
    `memory_batch` new messages have piled up, otherwise when the chat goes quiet."""
    if timer := _memory_timers.pop(conv_id, None):
        timer.cancel()
    if memory.unprocessed_user_messages(conv_id) >= CFG["memory_batch"]:
        run_in_background(memory.extract_from_conversation, conv_id)
        return

    async def when_idle():
        await asyncio.sleep(CFG["memory_idle_seconds"])
        _memory_timers.pop(conv_id, None)
        run_in_background(memory.extract_from_conversation, conv_id)

    _memory_timers[conv_id] = asyncio.create_task(when_idle())


@app.on_event("startup")
async def on_startup():
    auth.ensure_local_app_token(DATA_DIR / "local-app.token")
    assistant.start(asyncio.get_running_loop())  # the C++ engine first, then ears + voiceprints (assistant mode)
    run_in_background(memory.reindex)
    run_in_background(memory.file_unsorted)
    _background.add(asyncio.create_task(browser.warm()))  # headless browser ready before the first search
    # Chats left unreviewed when Dork last stopped get reviewed now.
    for conv in db.q("SELECT id FROM conversations"):
        if memory.unprocessed_user_messages(conv["id"]):
            schedule_memory_review(conv["id"])


# ---------- auth ----------

class LoginBody(BaseModel):
    key: str = Field(max_length=200)
    device_name: str = Field(default="Device", max_length=60)


class ChangeKeyBody(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(min_length=8, max_length=200)


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request):
    ip = auth.client_ip(request)
    auth.check_rate_limit(ip)
    if not auth.verify_master_key(body.key):
        auth.record_fail(ip)
        raise HTTPException(401, "Wrong master key.")
    token = auth.pair_device(body.device_name.strip(), ip)
    return _signed_in(token)


def _signed_in(token: str) -> JSONResponse:
    resp = JSONResponse({"ok": True})
    # Stays signed in for years; revoke a device from Settings if it's lost.
    resp.set_cookie(auth.COOKIE, token, max_age=60 * 60 * 24 * 3650, httponly=True, samesite="strict")
    return resp


class PairRequestBody(BaseModel):
    device_name: str = Field(default="Device", max_length=60)


class PairVerifyBody(BaseModel):
    request_id: str = Field(max_length=64)
    code: str = Field(max_length=20)


@app.post("/api/pair/request")
def pair_request(body: PairRequestBody, request: Request):
    return auth.request_pairing(body.device_name.strip(), auth.client_ip(request))


@app.post("/api/pair/verify")
def pair_verify(body: PairVerifyBody, request: Request):
    ip = auth.client_ip(request)
    auth.check_rate_limit(ip)
    try:
        token = auth.verify_pairing(body.request_id, body.code, ip)
    except HTTPException:
        auth.record_fail(ip)
        raise
    return _signed_in(token)


@app.get("/api/pair/pending")
def pair_pending(device=Depends(auth.require_device)):
    return auth.pending_pairings()


@app.delete("/api/pair/{request_id}")
def pair_deny(request_id: str, device=Depends(auth.require_device)):
    auth.deny_pairing(request_id)
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, device=Depends(auth.require_device)):
    db.x("DELETE FROM devices WHERE id=?", (device["id"],))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/auth/me")
def me(device=Depends(auth.require_device)):
    return {"device": {"id": device["id"], "name": device["name"]}, "profile": memory.get_profile()}


@app.post("/api/auth/change-key")
def change_key(body: ChangeKeyBody, request: Request, device=Depends(auth.require_device)):
    ip = auth.client_ip(request)
    auth.check_rate_limit(ip)
    if not auth.verify_master_key(body.current):
        auth.record_fail(ip)
        raise HTTPException(401, "Current master key is wrong.")
    auth.set_master_key(body.new)  # revokes every device, including this one
    return {"ok": True}


@app.get("/api/devices")
def devices(device=Depends(auth.require_device)):
    rows = db.q("SELECT id, name, ip, created_at, last_seen FROM devices ORDER BY last_seen DESC")
    for r in rows:
        r["current"] = r["id"] == device["id"]
    return rows


@app.delete("/api/devices/{device_id}")
def revoke_device(device_id: str, device=Depends(auth.require_device)):
    db.x("DELETE FROM devices WHERE id=?", (device_id,))
    return {"ok": True}


# ---------- status ----------

@app.get("/api/status")
async def status(device=Depends(auth.require_device)):
    return {
        "version": VERSION,
        "context_tokens": llm.CONTEXT_LIMIT,
        "auto_memory": CFG["auto_memory"],
        "web_search": CFG["web_search"],
        "memories": db.one("SELECT COUNT(*) AS n FROM memories")["n"],
        "devices": db.one("SELECT COUNT(*) AS n FROM devices")["n"],
        **(await llm.status()),
    }


# ---------- conversations ----------

class NewConversation(BaseModel):
    title: str = Field(default="New chat", max_length=120)


class RenameConversation(BaseModel):
    title: str = Field(max_length=120)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    web: str = Field(default="auto", pattern="^(auto|on|off)$")
    voice: bool = False  # the message was spoken: reply in a spoken style (short, emotion cues)


def _conversation(conv_id: str) -> dict:
    conv = db.one("SELECT * FROM conversations WHERE id=?", (conv_id,))
    if not conv:
        raise HTTPException(404, "Conversation not found.")
    return conv


@app.get("/api/conversations")
def list_conversations(device=Depends(auth.require_device)):
    return db.q("SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC")


@app.post("/api/conversations")
def create_conversation(body: NewConversation, device=Depends(auth.require_device)):
    conv_id = secrets.token_hex(8)
    db.x(
        "INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?,?,?,?)",
        (conv_id, body.title, db.now(), db.now()),
    )
    return _conversation(conv_id)


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str, device=Depends(auth.require_device)):
    conv = _conversation(conv_id)
    *_, stats, _ = context.build(conv, [])
    msgs = db.q(
        "SELECT id, role, content, device, created_at, meta FROM messages WHERE conversation_id=? ORDER BY id",
        (conv_id,),
    )
    for m in msgs:
        m["meta"] = json.loads(m["meta"]) if m["meta"] else None
    return {**conv, "messages": msgs, "context": stats}


@app.patch("/api/conversations/{conv_id}")
def rename_conversation(conv_id: str, body: RenameConversation, device=Depends(auth.require_device)):
    _conversation(conv_id)
    db.x("UPDATE conversations SET title=? WHERE id=?", (body.title.strip() or "Untitled", conv_id))
    return {"ok": True}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, device=Depends(auth.require_device)):
    db.x("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
    db.x("DELETE FROM conversations WHERE id=?", (conv_id,))
    return {"ok": True}


@app.post("/api/conversations/{conv_id}/chat")
async def chat(conv_id: str, body: ChatBody, device=Depends(auth.require_device)):
    conv = _conversation(conv_id)
    text = body.message.strip()

    first = db.one("SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?", (conv_id,))["n"] == 0
    user_msg_id = db.x(
        "INSERT INTO messages(conversation_id, role, content, device, created_at) VALUES(?,?,?,?,?)",
        (conv_id, "user", text, device["name"], db.now()),
    )
    if first and conv["title"] in ("New chat", ""):
        title = text.splitlines()[0][:60] + ("…" if len(text) > 60 else "")
        db.x("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
    db.x("UPDATE conversations SET updated_at=? WHERE id=?", (db.now(), conv_id))

    memories = await memory.recall(text)
    def save(reply: list[str], overflow: list[dict], meta: dict | None) -> int | None:
        full = "".join(reply).strip()
        if not full:
            return None
        msg_id = db.x(
            "INSERT INTO messages(conversation_id, role, content, device, created_at, meta) VALUES(?,?,?,?,?,?)",
            (conv_id, "assistant", full, "dork", db.now(), json.dumps(meta) if meta else None),
        )
        db.x("UPDATE conversations SET updated_at=? WHERE id=?", (db.now(), conv_id))
        if CFG["auto_memory"]:
            schedule_memory_review(conv_id)
        if len(overflow) >= context.SUMMARIZE_AFTER:
            run_in_background(context.summarize_overflow, conv_id, overflow)
        return msg_id

    def event(**e) -> str:
        return json.dumps(e) + "\n"

    async def stream():
        reply: list[str] = []
        overflow: list[dict] = []
        meta: dict | None = None
        saved = False
        waiting = True
        llm.chat_waiting += 1
        try:
            system, history, note, stats, overflow = context.build(conv, memories)
            yield event(type="meta", user_id=user_msg_id)
            mode = body.web if CFG["web_search"] else "off"
            async with llm.exclusive():
                llm.chat_waiting -= 1
                waiting = False
                async for kind, value in agent.run(system, history, note, mode, voice=body.voice):
                    if kind == "token":
                        reply.append(value)
                        yield event(type="token", text=value)
                    elif kind == "status":
                        yield event(type="status", text=value)
                    elif kind == "sources":
                        yield event(type="sources", sources=value)
                    elif kind == "done":
                        meta = value
            saved = True
            yield event(type="done", id=save(reply, overflow, meta))
        except Exception as e:  # noqa: BLE001 — surface any model failure to the UI
            log.exception("chat failed")
            saved = True
            save(reply, overflow, meta)
            yield event(type="error", error=str(e) or e.__class__.__name__)
        finally:
            # Client disconnected (e.g. phone locked): keep whatever was generated.
            if waiting:
                llm.chat_waiting -= 1
            if not saved:
                save(reply, overflow, meta)

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


# ---------- memories ----------

class MemoryBody(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    path: str = Field(default="", max_length=200)
    importance: int = Field(default=4, ge=1, le=5)


class MoveBody(BaseModel):
    path: str = Field(min_length=1, max_length=200)


class ProfileBody(BaseModel):
    name: str = Field(default="", max_length=100)
    call_me: str = Field(default="", max_length=100)
    about: str = Field(default="", max_length=3000)
    learn_from_chats: bool = True


class UploadBody(BaseModel):
    filename: str = Field(max_length=200)
    data_b64: str = Field(max_length=14_000_000)  # ~10 MB file


@app.get("/api/profile")
def get_profile(device=Depends(auth.require_device)):
    return memory.get_profile()


@app.put("/api/profile")
def update_profile(body: ProfileBody, device=Depends(auth.require_device)):
    return memory.set_profile(**body.model_dump())


@app.post("/api/profile/refresh")
async def refresh_profile(device=Depends(auth.require_device)):
    return await memory.refresh_gathered()


def _document_text(filename: str, data: bytes) -> str:
    if filename.lower().endswith(".pdf"):
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    return data.decode("utf-8", errors="ignore")


@app.post("/api/profile/upload")
async def upload_profile_document(body: UploadBody, device=Depends(auth.require_device)):
    if not body.filename.lower().endswith((".txt", ".md", ".pdf")):
        raise HTTPException(400, "Upload a .txt, .md or .pdf file.")
    try:
        text = _document_text(body.filename, base64.b64decode(body.data_b64))
    except ImportError:
        raise HTTPException(400, "PDF reading isn't installed yet (run: pip install pypdf). Upload a .txt or .md for now.")
    except Exception:  # noqa: BLE001 — any unreadable file gets the same answer
        raise HTTPException(400, "Couldn't read that file.")
    if len(text.strip()) < 20:
        raise HTTPException(400, "No readable text found in that file (scanned PDFs aren't supported yet).")
    learned = await memory.learn_from_document(body.filename, text)
    return {"learned": learned, "profile": memory.get_profile()}


@app.get("/api/memories")
def list_memories(device=Depends(auth.require_device)):
    return memory.list_all()


@app.post("/api/memories")
async def add_memory(body: MemoryBody, device=Depends(auth.require_device)):
    return await memory.add(body.content, body.path, body.importance, source="manual")


@app.patch("/api/memories/{memory_id}")
def move_memory(memory_id: int, body: MoveBody, device=Depends(auth.require_device)):
    memory.move(memory_id, body.path)
    return {"ok": True}


@app.get("/api/memory-tree")
def memory_tree(device=Depends(auth.require_device)):
    return {"roots": list(memory.ROOTS), "paths": memory.tree_paths(200)}


# ---------- greeting ----------

# ---------- voice ----------

class SpeakBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    whole: bool = False  # True: an expressive reply, spoken in one go by the expressive voice


@app.post("/api/voice/speak")
async def voice_speak(body: SpeakBody, device=Depends(auth.require_device)):
    out = await voice.speak(body.text, body.whole)
    if not out:
        raise HTTPException(503, "No voice available right now.")
    data, mime, engine = out
    return Response(data, media_type=mime, headers={"X-Voice-Engine": engine, "Cache-Control": "no-store"})


class HearBody(BaseModel):
    audio_b64: str = Field(max_length=8_000_000)  # ~6 MB of recorded audio
    mime: str = Field(default="audio/webm", max_length=60)


TRANSCRIBE = ("Transcribe this voice message exactly as spoken. Write Tamil words in Tamil script and English words "
              "in English; keep the speaker's mix of languages. Reply with only the transcript, nothing else. "
              "If nothing intelligible was said, reply with nothing.")


async def _hear_locally(audio_b64: str) -> str:
    """Offline speech-to-text (faster-whisper), used when Gemini isn't available."""
    import numpy as np

    try:
        raw = await asyncio.to_thread(hearing.decode, base64.b64decode(audio_b64))
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Couldn't read that recording.")
    segs = await asyncio.to_thread(hearing.speech_segments, raw)
    return await asyncio.to_thread(hearing.transcribe, np.concatenate(segs)) if segs else ""


@app.post("/api/voice/hear")
async def voice_hear(body: HearBody, device=Depends(auth.require_device)):
    """Speech → text for the desktop app (its browser engine has no speech recognition)."""
    key = CFG.get("gemini_api_key")
    if not key:
        return {"text": await _hear_locally(body.audio_b64), "engine": "local"}
    # Names Dork knows (you, people, projects, companies) so they're spelled right: "Droidal", not "Droydal".
    names = {memory.get_profile()["name"]} | {p.split("/")[-1].split(" (")[0] for p in memory.tree_paths(80)} \
        | {p.split("(")[-1].rstrip(")") for p in memory.tree_paths(80) if "(" in p}
    hint = TRANSCRIBE + (" Names that may come up: " + ", ".join(sorted(n for n in names if n)) + "." if names else "")
    payload = {"contents": [{"parts": [{"text": hint},
                                       {"inlineData": {"mimeType": body.mime.split(";")[0], "data": body.audio_b64}}]}]}
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            r = await http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{CFG['gemini_model']}:generateContent",
                                params={"key": key}, json=payload)
    except httpx.HTTPError:
        return {"text": await _hear_locally(body.audio_b64), "engine": "local"}
    if r.status_code != 200:  # quota / offline: transcribe on this PC instead
        return {"text": await _hear_locally(body.audio_b64), "engine": "local"}
    parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return {"text": " ".join(p.get("text", "") for p in parts).strip()}


@app.get("/api/greeting")
async def get_greeting(device=Depends(auth.require_device)):
    g = greeting.current()
    if g["pending"]:
        run_in_background(greeting.refresh)
    return g


class TruncateBody(BaseModel):
    from_message_id: int


@app.post("/api/conversations/{conv_id}/truncate")
def truncate(conv_id: str, body: TruncateBody, device=Depends(auth.require_device)):
    """Editing a message: drop it and everything after it; the client then resends the edited text."""
    conv = _conversation(conv_id)
    db.x("DELETE FROM messages WHERE conversation_id=? AND id>=?", (conv_id, body.from_message_id))
    if (conv["summarized_upto"] or 0) >= body.from_message_id:
        db.x("UPDATE conversations SET summary='', summarized_upto=0 WHERE id=?", (conv_id,))
    return {"ok": True}


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, device=Depends(auth.require_device)):
    db.x("DELETE FROM memories WHERE id=?", (memory_id,))
    return {"ok": True}


# ---------- assistant mode (voice-only, see dork/assistant.py) ----------

@app.on_event("shutdown")
def on_shutdown():
    assistant.stop()
    from . import engine
    engine.stop()


@app.get("/api/assistant/events")
async def assistant_events(request: Request, host: bool = False, device=Depends(auth.require_device)):
    """Server-sent events for the reactor UI: state, mic level, captions, what to say, UI actions."""
    cid, q = assistant.subscribe(host)

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'hello', 'client': cid, **assistant.snapshot()})}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                if await request.is_disconnected():
                    break
        finally:
            assistant.unsubscribe(cid)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/assistant/state")
def assistant_state(device=Depends(auth.require_device)):
    return {**assistant.snapshot(), "voice_conversation": assistant.voice_conversation()["id"]}


class UtteranceBody(BaseModel):
    audio_b64: str = Field(default="", max_length=8_000_000)
    mime: str = Field(default="audio/webm", max_length=60)
    text: str = Field(default="", max_length=2000)  # typed instead of spoken
    client: str = Field(default="", max_length=20)


@app.post("/api/assistant/utterance")
async def assistant_utterance(body: UtteranceBody, device=Depends(auth.require_device)):
    """A phrase recorded on a phone/browser (or typed): same pipeline as the PC's own microphone."""
    import numpy as np

    samples = np.zeros(0, np.float32)
    if body.audio_b64:
        try:
            raw = await asyncio.to_thread(hearing.decode, base64.b64decode(body.audio_b64))
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Couldn't read that recording.")
        segs = await asyncio.to_thread(hearing.speech_segments, raw)
        samples = np.concatenate(segs) if segs else np.zeros(0, np.float32)
        if not len(samples):
            return {"heard": ""}
    elif not body.text.strip():
        raise HTTPException(400, "Say or type something.")
    return await assistant.handle(samples, source="device", to=body.client or None, device_trusted=True,
                                  text=body.text.strip() or None)


class SpokenBody(BaseModel):
    playing: bool


@app.post("/api/assistant/spoken")
def assistant_spoken(body: SpokenBody, device=Depends(auth.require_device)):
    assistant.spoken(body.playing)
    return {"ok": True}


class HostMicBody(BaseModel):
    on: bool


@app.post("/api/assistant/hostmic")
def assistant_hostmic(body: HostMicBody, device=Depends(auth.require_device)):
    """Heartbeat from the assistant window while its echo-cancelled conversation mic is on."""
    assistant.S.host_mic_until = (__import__("time").time() + 10) if body.on else 0
    return {"ok": True}


class ClientBody(BaseModel):
    client: str = Field(default="", max_length=20)


@app.post("/api/assistant/wake")
async def assistant_wake(body: ClientBody, device=Depends(auth.require_device)):
    await assistant.wake_from_ui(body.client or None)
    return assistant.snapshot()


@app.post("/api/assistant/sleep")
def assistant_sleep(device=Depends(auth.require_device)):
    assistant.S.awake_until = 0
    assistant.S.state = "sleeping"
    assistant.emit({"type": "state", "state": "sleeping", "locked": assistant.S.locked})
    return assistant.snapshot()


@app.get("/api/voiceprints")
def voiceprints(device=Depends(auth.require_device)):
    log_rows = db.q("SELECT at, kind, speaker, score, ok, detail FROM voice_auth_log ORDER BY id DESC LIMIT 20")
    return {**hearing.ready(), "threshold": CFG["voice_match_threshold"], "log": log_rows}


class EnrollBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    owner: bool = False
    clips_b64: list[str] = Field(min_length=1, max_length=12)


@app.post("/api/voiceprints/enroll")
async def voiceprint_enroll(body: EnrollBody, device=Depends(auth.require_device)):
    """Enroll from recordings made in Settings (each clip: one spoken sentence)."""
    import numpy as np

    clips = []
    for b64 in body.clips_b64:
        try:
            raw = await asyncio.to_thread(hearing.decode, base64.b64decode(b64))
        except Exception:  # noqa: BLE001
            continue
        segs = await asyncio.to_thread(hearing.speech_segments, raw)
        if segs:
            clips.append(np.concatenate(segs))
    try:
        r = await asyncio.to_thread(hearing.enroll, body.name.strip(), clips, "owner" if body.owner else "guest")
    except ValueError as e:
        raise HTTPException(400, str(e))
    hearing.log_auth("enroll", {"name": r["name"], "score": r["consistency"]}, True, f"settings, {r['samples']} samples")
    return r


@app.post("/api/voiceprints/identify")
async def voiceprint_identify(body: UtteranceBody, device=Depends(auth.require_device)):
    import numpy as np

    raw = await asyncio.to_thread(hearing.decode, base64.b64decode(body.audio_b64))
    segs = await asyncio.to_thread(hearing.speech_segments, raw)
    if not segs:
        return {"name": None, "score": 0, "known": False, "heard": False}
    return await asyncio.to_thread(hearing.identify, np.concatenate(segs))


@app.delete("/api/voiceprints/{name}")
def voiceprint_forget(name: str, device=Depends(auth.require_device)):
    hearing.forget(name)
    return {"ok": True}


# ---------- web UI ----------

@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/assistant")
def assistant_page():
    return FileResponse(WEB_DIR / "assistant.html", headers={"Cache-Control": "no-cache"})


@app.get("/progress")
def progress_page():
    """Build progress, readable from a phone (plain text)."""
    path = DATA_DIR / "nano2_progress.md"
    text = path.read_text(encoding="utf-8") if path.exists() else "No progress file."
    return Response(text, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
