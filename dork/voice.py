"""Dork's voice, in three tiers:

  1. Leda (Gemini TTS) — expressive: laughs, sighs, excitement, Tamil. Used for replies that carry emotion
     cues like [laughs]. The free tier has a small daily limit per model, so Dork rotates through the
     Gemini speech models (each has its own limit) and remembers which ones are used up for the day.
  2. Neerja / Pallavi (Microsoft neural voices) — fluent and unlimited, online. Neerja (Indian English) for
     English, Pallavi for Tamil, Swara for Hindi; chosen per sentence.
  3. Kokoro (local, offline) — when there's no internet.

Everything spoken is cached on disk, so repeated lines cost nothing."""

import asyncio
import base64
import datetime
import hashlib
import io
import json
import logging
import re
import threading

import httpx
import numpy as np
from pathlib import Path

from . import db
from .config import CFG, DATA_DIR, ROOT

log = logging.getLogger("dork.voice")

# ---------------------------------------------------------------- text preparation

CUE = re.compile(r"\[(laugh(s|ing)?|chuckles?|giggles?|sigh(s|ing)?|excited|happy|cheerful|thinking|hmm|"
                 r"annoyed|irritated|angry|sad|softly|whisper(s|ing)?|surprised|teasing|serious|warmly)[^\]]*\]", re.I)
TAMIL = re.compile(r"[஀-௿]")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def speakable(text: str, keep_cues: bool = False) -> str:
    """Markdown reply → plain spoken text (optionally keeping [emotion] cues for the expressive voice)."""
    text = re.sub(r"```.*?```", " (the code is on screen) ", text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[(\d{1,2})\]", "", text)                       # citations
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text)  # links → their text
    text = re.sub(r"https?://\S+", "the link on screen", text)
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"[*_~]+", "", text)
    if not keep_cues:
        text = re.sub(r"\[(laugh(s|ing)?|chuckles?|giggles?)[^\]]*\]", "Haha,", text, flags=re.I)
        text = CUE.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def is_expressive(text: str) -> bool:
    return bool(CUE.search(text))


def sentences(text: str) -> list[str]:
    out = []
    for para in [p.strip() for p in re.split(r"\n+", text) if p.strip()]:
        out += [s.strip() for s in re.split(r"(?<=[.!?…।])\s+", para) if s.strip()]
    return out


# ---------------------------------------------------------------- cache

CACHE_DIR = DATA_DIR / "voice_cache"


def _cache_path(engine: str, voice: str, text: str, ext: str):
    h = hashlib.sha1(f"{engine}|{voice}|{text}".encode()).hexdigest()[:20]
    return CACHE_DIR / f"{h}.{ext}"


def _cached(engine: str, voice: str, text: str, ext: str) -> bytes | None:
    p = _cache_path(engine, voice, text, ext)
    return p.read_bytes() if p.exists() else None


def _store(engine: str, voice: str, text: str, ext: str, data: bytes) -> bytes:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(engine, voice, text, ext).write_bytes(data)
    return data


# ---------------------------------------------------------------- tier 1: Leda (Gemini)

GEMINI_MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview",
                 "gemini-2.5-flash-preview-tts")
DIRECTION = ("Perform this as Dork: a warm, witty friend who switches naturally between English and Tamil. "
             "Follow the cues in square brackets for laughs, hesitation and emotion and never read them aloud. "
             "Sound like a real person talking, not someone reading.")


def _quota_reset() -> float:
    """Gemini daily quotas reset at midnight Pacific time (~08:00 UTC)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    reset = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += datetime.timedelta(days=1)
    return reset.timestamp()


def _exhausted() -> dict:
    data = json.loads(db.get_meta("tts_exhausted") or "{}")
    return {m: t for m, t in data.items() if t > db.now()}


def _mark_exhausted(model: str) -> None:
    data = _exhausted()
    data[model] = _quota_reset()
    db.set_meta("tts_exhausted", json.dumps(data))
    log.info("voice: %s used up for today", model)


async def _gemini(text: str) -> bytes | None:
    key = CFG.get("gemini_api_key")
    if not key:
        return None
    if (hit := _cached("gemini", CFG["voice_expressive"], text, "wav")) is not None:
        return hit
    used_up = _exhausted()
    async with httpx.AsyncClient(timeout=120) as http:
        for model in GEMINI_MODELS:
            if model in used_up:
                continue
            body = {"contents": [{"parts": [{"text": f"{DIRECTION}\n\n{text}"}]}],
                    "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": CFG["voice_expressive"]}}}}}
            try:
                r = await http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                    params={"key": key}, json=body)
            except httpx.HTTPError as e:
                log.info("voice: gemini unreachable (%s)", e.__class__.__name__)
                return None
            if r.status_code == 429:
                if "PerDay" in r.text or "per day" in r.text.lower() or "daily" in r.text.lower():
                    _mark_exhausted(model)
                continue  # per-minute limit or used up: try the next model
            if r.status_code != 200:
                log.info("voice: %s error %s", model, r.status_code)
                continue
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            data, mime = base64.b64decode(part["data"]), part.get("mimeType", "")
            if "wav" not in mime:  # raw 16-bit PCM → WAV
                rate = int(mime.split("rate=")[-1].split(";")[0]) if "rate=" in mime else 24000
                data = _wav(np.frombuffer(data, dtype=np.int16), rate)
            return _store("gemini", CFG["voice_expressive"], text, "wav", data)
    return None


# ---------------------------------------------------------------- tier 2: Neerja / Pallavi (Microsoft)

def _ms_voice(sentence: str) -> str:
    if TAMIL.search(sentence):
        return CFG["voice_tamil"]
    if DEVANAGARI.search(sentence):
        return CFG["voice_hindi"]
    return CFG["voice_everyday"]


async def _microsoft(sentence: str) -> bytes | None:
    voice = _ms_voice(sentence)
    if (hit := _cached("ms", voice, sentence, "mp3")) is not None:
        return hit
    try:
        import edge_tts

        audio = b""
        async for chunk in edge_tts.Communicate(sentence, voice).stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
        return _store("ms", voice, sentence, "mp3", audio) if audio else None
    except Exception as e:  # noqa: BLE001 — offline or service change: fall back to Kokoro
        log.info("voice: microsoft voice unavailable (%s)", e.__class__.__name__)
        return None


# ---------------------------------------------------------------- tier 3: Kokoro (offline)

MODEL_DIR = Path(CFG.get("models_dir") or ROOT / "models") / "kokoro"
_kokoro = None
_klock = threading.Lock()


def _wav(samples: np.ndarray, rate: int) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, samples, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _kokoro_sync(sentence: str) -> bytes | None:
    global _kokoro
    model = MODEL_DIR / "kokoro-v1.0.onnx"
    if not model.exists():
        return None
    with _klock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro

            _kokoro = Kokoro(str(model), str(MODEL_DIR / "voices-v1.0.bin"))
        samples, rate = _kokoro.create(sentence, voice=CFG["voice_offline"], speed=1.0, lang="en-us")
    return _wav(samples, rate)


async def _offline(sentence: str) -> bytes | None:
    if TAMIL.search(sentence) or DEVANAGARI.search(sentence):
        return None  # the offline voice only speaks English
    if (hit := _cached("kokoro", CFG["voice_offline"], sentence, "wav")) is not None:
        return hit
    data = await asyncio.to_thread(_kokoro_sync, sentence)
    return _store("kokoro", CFG["voice_offline"], sentence, "wav", data) if data else None


# ---------------------------------------------------------------- public

async def speak(text: str, whole: bool) -> tuple[bytes, str, str] | None:
    """→ (audio bytes, mime type, engine) or None.
    whole=True: an expressive reply — one Gemini request for all of it (falls back to plain speech of it).
    whole=False: one sentence of an everyday reply."""
    if whole and is_expressive(text):
        if data := await _gemini(speakable(text, keep_cues=True)):
            return data, "audio/wav", "expressive"
    plain = speakable(text)
    if not plain:
        return None
    if data := await _microsoft(plain):
        return data, "audio/mpeg", "everyday"
    if data := await _offline(plain):
        return data, "audio/wav", "offline"
    return None


def status() -> dict:
    left = [m for m in GEMINI_MODELS if m not in _exhausted()]
    return {"expressive_models_left": len(left), "offline_ready": (MODEL_DIR / "kokoro-v1.0.onnx").exists()}
