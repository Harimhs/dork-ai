"""Configuration. Defaults below; override any key in data/config.json."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DORK_DATA", ROOT / "data"))
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "dork.db"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 7777,
    "provider": "gemini",       # "gemini" | "groq" | "anthropic" (Claude) | "ollama" (local, offline)
    "context_tokens": 16000,    # history budget per message (local: the model's context window)
    "cloud_max_tokens": 8000,   # reply length cap for gemini / groq

    # Google Gemini (provider = gemini) — key from aistudio.google.com
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "gemini_background_model": "gemini-2.5-flash-lite",   # memory + summaries; separate free quota
    "gemini_embed_model": "gemini-embedding-001",
    "gemini_embed_dims": 768,
    "gemini_reasoning": "low",  # none | low | medium | high

    # Groq (provider = groq) — key from console.groq.com
    "groq_api_key": "",
    "groq_model": "llama-3.3-70b-versatile",

    # Claude (provider = anthropic)
    "anthropic_api_key": "",    # or set the ANTHROPIC_API_KEY env var
    "claude_model": "claude-opus-5-5",
    "claude_effort": "low",     # low | medium | high — higher = smarter, slower, pricier
    "claude_max_tokens": 8000,

    # Local model (provider = ollama)
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:3b",
    "ollama_max_tokens": 1024,
    "ollama_keep_alive": "24h",  # keep the model in RAM: no slow first reply after idle
    "models_dir": "",            # Whisper / voiceprint / Kokoro models ("" = <repo>/models); set by setup
    "ollama_models": "",         # where Ollama's models are ("" = $OLLAMA_MODELS or the OS default); set by setup
    "llamacpp_exe": "",          # llama.cpp server binary ("" = found automatically); set by setup
    "local_engine": "ollama",   # "ollama", or "llamacpp": llama.cpp server + speculative decoding (~20% faster)
    "llamacpp_port": 8091,
    "llamacpp_draft": "",       # e.g. "qwen2.5:0.5b": speculative decoding (measured: no gain for short voice replies)
    "llamacpp_embed": True,     # embeddings on llama.cpp too (port below), so no model call goes through Ollama
    "llamacpp_embed_port": 8092,
    "ollama_threads": 8,         # all logical cores: ~35% faster writing on Hari's i5 than Ollama's default
    "embed_model": "nomic-embed-text",

    "memory_top_k": 5,          # relevant memories injected per message (bounded, however many exist)
    "memory_min_importance": 3, # 1-5; facts rated below this are not stored
    "memory_batch": 4,          # review a chat for memories after this many new messages from you...
    "memory_idle_seconds": 120, # ...or after this long without a new message
    "auto_memory": True,
    "web_search": True,         # Dork Search: look things up on the web when a message needs it
    "creator_name": "Hari",     # part of Dork's identity ("built by …"), separate from the user profile
    # Voice (see dork/voice.py): expressive → everyday → offline
    "voice_expressive": "Leda",                       # Gemini TTS voice, for replies with emotion cues
    "voice_everyday": "en-IN-NeerjaExpressiveNeural",  # Microsoft, English
    "voice_tamil": "ta-IN-PallaviNeural",              # Microsoft, Tamil sentences
    "voice_hindi": "hi-IN-SwaraNeural",                # Microsoft, Hindi sentences
    "voice_offline": "af_heart",                       # Kokoro, when there's no internet

    # Assistant mode (see dork/assistant.py, dork/hearing.py)
    "voice_brain": "gemini",        # assistant turns: "gemini" (fast, needs internet) or "local"
    "voice_reasoning": "none",
    "voice_gemini_model": "gemini-2.5-flash-lite",  # voice turns: fastest Gemini, bigger free daily allowance
    "voice_natural": False,         # extra Gemini call to phrase action confirmations (costs quota + ~2 s)      # Gemini thinking for voice turns: none = fastest
    "voice_stt": "gemini",          # once awake: "gemini" (accurate, Tamil) or "local" (Whisper, offline)
    "stt_trust": -0.25,             # offline transcript confidence above which Gemini isn't asked (faster)
    "ears": True,                   # listen on this PC's microphone in the background
    "ears_gain": 3.0,               # mic boost before listening (quiet laptop mics); 1 = as is
    "vad_threshold": 0.4,           # speech detection sensitivity (0-1, lower = more sensitive)
    "ears_device": "",              # part of a mic's name to listen with (e.g. "Rockerz"); "" = Windows default
    "stt_model": "base.en",         # faster-whisper model in models/whisper (tiny.en = faster, less accurate)
    "voice_match_threshold": 0.6,   # voiceprint similarity needed to recognise someone (0-1)
    "voice_owner_only": True,       # once Hari's voice is enrolled, only his voice can wake Dork / control the PC
    "assistant_sleep_after": 60,    # seconds of quiet before the assistant goes back to sleep (then it needs "Dork")
}


def load() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
    return cfg


def save(**changes) -> None:
    """Persist changes to data/config.json (takes effect on restart)."""
    stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    stored.update(changes)
    CONFIG_PATH.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    CFG.update(changes)


CFG = load()
