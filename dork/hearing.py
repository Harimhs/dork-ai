"""Dork's hearing, fully offline: speech detection, transcription and voiceprints.

    audio → VAD (Silero) → speech segments → voiceprint (who is speaking?) → Whisper (what did they say?)

Voiceprints are speaker embeddings (3D-Speaker ERes2Net, run with onnxruntime via sherpa-onnx).
Enrolling = averaging the embeddings of a few phrases; recognising = cosine similarity against every
enrolled person. Everything stays in data/ on this PC."""

import re
import io
import json
import logging
import threading
import time

import numpy as np
from pathlib import Path

from . import db
from .config import CFG, ROOT

log = logging.getLogger("dork.hearing")

RATE = 16000
MODELS = Path(CFG.get("models_dir") or ROOT / "models")
SPEAKER_MODEL = MODELS / "speaker" / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
VAD_MODEL = MODELS / "speaker" / "silero_vad.onnx"
WHISPER_DIR = MODELS / "whisper"

db.x("""CREATE TABLE IF NOT EXISTS voiceprints (
    name TEXT PRIMARY KEY, role TEXT, embedding TEXT, samples INTEGER, created_at REAL, updated_at REAL)""")
db.x("""CREATE TABLE IF NOT EXISTS voice_auth_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, kind TEXT, speaker TEXT, score REAL, ok INTEGER, detail TEXT)""")

_lock = threading.Lock()
_extractor = None
_whisper = None


# ---------------------------------------------------------------- audio helpers

def decode(data: bytes) -> np.ndarray:
    """Any audio file (webm/ogg/mp3/wav) → mono float32 at 16 kHz."""
    import av

    container = av.open(io.BytesIO(data))
    resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
    out = []
    for frame in container.decode(audio=0):
        for f in resampler.resample(frame):
            out.append(f.to_ndarray().reshape(-1))
    for f in resampler.resample(None):
        out.append(f.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)


def level(samples: np.ndarray) -> float:
    """Loudness 0..1 for the reactor (RMS, roughly perceptual)."""
    if not len(samples):
        return 0.0
    rms = float(np.sqrt(np.mean(samples * samples)))
    return float(min(1.0, max(0.0, (20 * np.log10(rms + 1e-6) + 55) / 45)))


def vad():
    """A fresh Silero voice-activity detector (one per audio stream)."""
    import sherpa_onnx

    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(VAD_MODEL)
    cfg.silero_vad.threshold = CFG["vad_threshold"]  # lower = catches quieter / farther speech
    cfg.silero_vad.min_silence_duration = 0.55   # pause that ends a phrase
    cfg.silero_vad.min_speech_duration = 0.2     # a quick "Dork" is ~0.25 s
    cfg.silero_vad.max_speech_duration = 15
    cfg.sample_rate = RATE
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)


def speech_segments(samples: np.ndarray) -> list[np.ndarray]:
    """Split a recording into speech segments (drops silence/noise)."""
    det = vad()
    out = []
    for i in range(0, len(samples), 512):
        det.accept_waveform(samples[i:i + 512])
        while not det.empty():
            out.append(np.array(det.front.samples, dtype=np.float32))
            det.pop()
    det.flush()
    while not det.empty():
        out.append(np.array(det.front.samples, dtype=np.float32))
        det.pop()
    return out


# ---------------------------------------------------------------- voiceprints

def _embedder():
    global _extractor
    if _extractor is None:
        import sherpa_onnx

        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(SPEAKER_MODEL), num_threads=2))
    return _extractor


def embed(samples: np.ndarray) -> np.ndarray | None:
    """Voiceprint of one stretch of speech (needs ~0.8 s or more to mean anything)."""
    if len(samples) < RATE * 0.6:
        return None
    with _lock:
        ex = _embedder()
        s = ex.create_stream()
        s.accept_waveform(RATE, samples)
        s.input_finished()
        e = np.array(ex.compute(s), dtype=np.float32)
    n = np.linalg.norm(e)
    return e / n if n else None


SELF = "Dork (own voice)"


async def learn_own_voice() -> None:
    """Enroll Dork's OWN speaking voice (role 'self'), so its replies coming back through the mic are ignored."""
    import asyncio

    from . import voice

    voices = {CFG["voice_everyday"]}
    row = db.one("SELECT embedding FROM voiceprints WHERE name=?", (SELF,))
    if row and db.get_meta("self_voice") == json.dumps(sorted(voices)):
        return
    lines = ["Right here. What do you need?", "Opening Chrome for you now.", "Timer set for ten minutes, I'll remind you.",
             "Hmm, let me think about that for a second.", "Done. Anything else you want me to handle today?"]
    clips = []
    for line in lines:
        out = await voice.speak(line, False)
        if out:
            try:
                clips.append(await asyncio.to_thread(decode, out[0]))
            except Exception:  # noqa: BLE001
                pass
    if len(clips) >= 3:
        forget(SELF)
        await asyncio.to_thread(enroll, SELF, clips, "self")
        db.set_meta("self_voice", json.dumps(sorted(voices)))
        log.info("hearing: learned Dork's own voice from %d clips", len(clips))


def people() -> list[dict]:
    rows = db.q("SELECT name, role, embedding, samples, updated_at FROM voiceprints ORDER BY role='owner' DESC, name")
    for r in rows:
        r["vec"] = np.array(json.loads(r.pop("embedding")), dtype=np.float32)
    return rows


def owner() -> dict | None:
    return next((p for p in people() if p["role"] == "owner"), None)


def enroll(name: str, clips: list[np.ndarray], role: str = "guest") -> dict:
    """Average the voiceprints of a few phrases into one stored voiceprint (adds to an existing one)."""
    vecs = [v for v in (embed(c) for c in clips) if v is not None]
    if not vecs:
        raise ValueError("Those recordings were too short or too quiet. Speak a full sentence each time.")
    old = db.one("SELECT embedding, samples FROM voiceprints WHERE name=?", (name,))
    total = np.sum(vecs, axis=0)
    n = len(vecs)
    if old:
        total += np.array(json.loads(old["embedding"])) * old["samples"]
        n += old["samples"]
    vec = total / n
    vec = vec / np.linalg.norm(vec)
    if role == "owner":  # only one owner
        db.x("UPDATE voiceprints SET role='guest' WHERE role='owner' AND name<>?", (name,))
    now = time.time()
    db.x("INSERT OR REPLACE INTO voiceprints(name, role, embedding, samples, created_at, updated_at) VALUES(?,?,?,?,"
         "COALESCE((SELECT created_at FROM voiceprints WHERE name=?), ?),?)",
         (name, role, json.dumps([round(float(x), 6) for x in vec]), n, name, now, now))
    # Consistency of the new phrases with each other: tells the UI whether enrollment went well.
    spread = float(np.mean([v @ vec for v in vecs]))
    return {"name": name, "role": role, "samples": n, "consistency": round(spread, 3)}


def forget(name: str) -> None:
    db.x("DELETE FROM voiceprints WHERE name=?", (name,))


def identify(samples: np.ndarray, scan: bool = True) -> dict:
    """Who is speaking? → {name, role, score, known}. Unknown voices get name None."""
    vec = embed(samples)
    if vec is None:
        return {"name": None, "role": None, "score": 0.0, "known": False, "short": True}
    best, score = None, -1.0
    for p in people():
        s = float(vec @ p["vec"])
        if s > score:
            best, score = p, s
    th = CFG["voice_match_threshold"]
    known = best is not None and score >= th
    # Several people talking over each other blur the whole-phrase voiceprint. Look at short windows too:
    # if one stretch is clearly an enrolled voice (stricter bar), that person was speaking.
    if scan and not known and len(samples) > RATE * 2.2 and people():
        win, step = int(RATE * 1.5), int(RATE * 0.5)
        for start in range(0, len(samples) - win + 1, step):
            v = embed(samples[start:start + win])
            if v is None:
                continue
            for p in people():
                s = float(v @ p["vec"])
                if s >= th + 0.05 and s > score:
                    best, score, known = p, s, True
    return {"name": best["name"] if known else None, "role": best["role"] if known else None,
            "score": round(score, 3), "known": known, "short": False}


def log_auth(kind: str, who: dict, ok: bool, detail: str = "") -> None:
    db.x("INSERT INTO voice_auth_log(at, kind, speaker, score, ok, detail) VALUES(?,?,?,?,?,?)",
         (time.time(), kind, who.get("name") or "unknown", who.get("score", 0), int(ok), detail[:200]))


# ---------------------------------------------------------------- transcription

def _model():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        _whisper = WhisperModel(CFG["stt_model"], device="cpu", compute_type="int8", download_root=str(WHISPER_DIR),
                                cpu_threads=4)
    return _whisper


def names_hint() -> str:
    """Names Dork knows, so Whisper spells them right ("Dork", "Hari", "Droidal")."""
    from . import memory

    p = memory.get_profile()
    names = {"Dork", p["name"], p["call_me"], *(r["name"] for r in people() if r["role"] != "self")}
    hint = "Dork, " + ", ".join(sorted(n for n in names if n and n != "Dork")) + "."
    # App names too, so "open Notepad" isn't heard as "open North path": what's open now, the everyday
    # ones, then the shortest installed names — phrased like commands, which is what Whisper expects next.
    try:
        from . import desktop

        # "img1.png - Notepad" → "Notepad": the app name comes last in a window title
        open_now = [re.split(r"\s[-–—|]\s", w.get("title", ""))[-1] for w in desktop.windows()][:10]
        common = ["Notepad", "Chrome", "VS Code", "Calculator", "File Explorer", "Spotify", "Settings", "YouTube"]
        installed = sorted((a["name"] for a in desktop.apps() if "(" not in a["name"] and len(a["name"].split()) <= 2),
                           key=len)
        seen, apps = set(), []
        for n in open_now + common + installed:
            n = str(n).strip()
            if n and n.lower() not in seen and len(n) <= 24:
                seen.add(n.lower())
                apps.append(n)
        hint += " Open " + ", ".join(apps[:40]) + ". Close this window. Minimise that."
    except Exception:  # noqa: BLE001 — the names are a nicety
        pass
    return hint


CLOUD_PROMPT = ("Transcribe this short voice command to a personal assistant named Dork, exactly as spoken. "
                "Write Tamil words in Tamil script and English words in English, keeping the speaker's mix. "
                "Reply with only the transcript. If no clear speech is addressed to the assistant (silence, noise, "
                "a TV or video in the background), reply with nothing. Words that may come up: ")


async def transcribe_cloud(samples: np.ndarray) -> str | None:
    """Gemini transcription for requests once Dork is awake: much better with accents, distance, Tamil and
    Tanglish than the small offline model. None = unavailable (offline / quota) → caller uses Whisper."""
    import base64

    import httpx
    import soundfile as sf

    from .config import CFG

    from . import llm

    key = CFG.get("gemini_api_key")
    if not key or len(samples) < RATE * 0.3 or llm.cloud_cooling():
        return None
    buf = io.BytesIO()
    sf.write(buf, samples, RATE, format="WAV", subtype="PCM_16")
    body = {"contents": [{"parts": [{"text": CLOUD_PROMPT + names_hint()},
                                    {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(buf.getvalue()).decode()}}]}],
            "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}}}
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{CFG['gemini_model']}:generateContent",
                                params={"key": key}, json=body)
        if r.status_code != 200:
            log.info("cloud transcription unavailable (%s)", r.status_code)
            if r.status_code == 429:
                llm.cloud_failed(RuntimeError("rate limit (transcription)"))
            return None
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return " ".join(p.get("text", "") for p in parts).strip()
    except Exception as e:  # noqa: BLE001
        log.info("cloud transcription failed: %s", e.__class__.__name__)
        return None


def transcribe(samples: np.ndarray) -> str:
    return transcribe_scored(samples)[0]


def transcribe_scored(samples: np.ndarray) -> tuple[str, float]:
    """Offline transcription plus Whisper's own confidence (mean token log-probability: ~-0.1 sure, < -0.5 guessing)."""
    if len(samples) < RATE * 0.3:
        return "", -9.0
    with _lock:
        segs, _ = _model().transcribe(samples, beam_size=1, language="en", vad_filter=False,
                                      initial_prompt=names_hint(), condition_on_previous_text=False)
        kept = [s for s in segs if s.no_speech_prob < 0.7]
        text = " ".join(s.text for s in kept).strip()
        conf = min((s.avg_logprob for s in kept), default=-9.0)
    # Whisper's habit on noise: hallucinated filler lines.
    if text.lower().strip(" .!?") in ("", "you", "thank you", "thanks for watching", "bye", "okay"):
        return "", -9.0
    return text, conf


def ready() -> dict:
    return {"speaker_model": SPEAKER_MODEL.exists(), "vad_model": VAD_MODEL.exists(),
            "stt_model": any(WHISPER_DIR.glob(f"models--Systran--faster-whisper-{CFG['stt_model']}/snapshots/*/model.bin")),
            "enrolled": [{"name": p["name"], "role": p["role"], "samples": p["samples"]} for p in people() if p["role"] != "self"],
            "knows_own_voice": any(p["role"] == "self" for p in people())}


def warm() -> None:
    """Load the models up front so the first phrase isn't slow."""
    try:
        _embedder()
        _model()
    except Exception:  # noqa: BLE001
        log.exception("hearing: model load failed")
